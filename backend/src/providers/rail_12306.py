"""直接访问 12306 公共查询接口的铁路客户端。

ZLAgent 的 12306 实现采用的是公开查询接口，而不是必须单独启动的本地 MCP 服务。
这里把它包装成 ``RailProvider`` 所需的 ``call_tool`` 接口，因此上层图节点不需要知道
铁路数据来自 MCP 还是 12306。这个文件只负责 HTTP、车站编码和 12306 的竖线格式解析，
车次事实模型仍由 ``providers.rail`` 统一生成。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from .base import ProviderError

STATION_NAME_URL = "https://kyfw.12306.cn/otn/resources/js/framework/station_name.js"
QUERY_ENDPOINTS = ("queryG", "queryZ", "query")
BASE_URL = "https://kyfw.12306.cn"
MAX_RESULTS = 20
CITY_STATION_PREFERENCES: dict[str, tuple[str, ...]] = {
    "北京": ("北京南", "北京", "北京西", "北京朝阳", "北京北"),
    "上海": ("上海虹桥", "上海", "上海南", "上海西"),
    "广州": ("广州南", "广州", "广州东", "广州白云"),
    "深圳": ("深圳北", "深圳", "福田", "深圳坪山"),
    "杭州": ("杭州东", "杭州西", "杭州"),
    "南京": ("南京南", "南京"),
    "武汉": ("武汉", "汉口", "武昌"),
    "成都": ("成都东", "成都西", "成都南", "成都"),
    "西安": ("西安北", "西安"),
}


class Public12306Client:
    """把 12306 公共接口适配成最小的 MCP 风格工具客户端。"""

    def __init__(
        self,
        *,
        timeout_seconds: float = 8,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124.0 TravelGraph/1.0"
            ),
            "Referer": f"{BASE_URL}/otn/leftTicket/init",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        self.http = http or httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=True,
            trust_env=True,
            headers=headers,
        )
        self._owns_http = http is None
        self._headers = headers
        self._stations: dict[str, str] | None = None
        self._station_lock = asyncio.Lock()
        self._bundles: dict[str, tuple[float, dict[str, Any]]] = {}
        self._bundle_locks: dict[str, asyncio.Lock] = {}

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """执行 RailProvider 约定的三个只读工具。"""

        if name == "search-stations":
            return await self._search_stations(arguments)
        if name in {"query-tickets", "query-ticket-price"}:
            bundle = await self._query_bundle(arguments)
            if name == "query-tickets":
                return {"trains": bundle["trains"]}
            return {"data": bundle["prices"]}
        raise ProviderError("rail_tool_not_found", f"12306 不支持工具：{name}")

    async def aclose(self) -> None:
        """关闭本客户端创建的连接池。"""

        if self._owns_http:
            await self.http.aclose()

    async def _search_stations(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return {"stations": []}
        stations = await self._station_codes()
        normalized = query.removesuffix("市")
        matches = [
            {"name": name, "code": code}
            for name, code in stations.items()
            if name == query or name == normalized or name.startswith(normalized)
        ]
        preferred = CITY_STATION_PREFERENCES.get(normalized, ())
        matches.sort(
            key=lambda item: preferred.index(item["name"])
            if item["name"] in preferred
            else 999
        )
        limit = _positive_int(arguments.get("limit"), 10)
        return {"stations": matches[:limit]}

    async def _query_bundle(self, arguments: dict[str, Any]) -> dict[str, Any]:
        origin = str(arguments.get("from_station") or "").strip().upper()
        destination = str(arguments.get("to_station") or "").strip().upper()
        travel_date = str(arguments.get("train_date") or "").strip()
        if not origin or not destination or not travel_date:
            raise ProviderError("rail_invalid_arguments", "12306 查询缺少车站或日期")
        key = f"{origin}:{destination}:{travel_date}"
        cached = self._bundles.get(key)
        if cached is not None:
            expires_at, bundle = cached
            if expires_at > time.monotonic():
                return bundle
            self._bundles.pop(key, None)
        lock = self._bundle_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._bundles.get(key)
            if cached is None or cached[0] <= time.monotonic():
                self._bundles[key] = (
                    time.monotonic() + 60,
                    await self._fetch_bundle(origin, destination, travel_date),
                )
        return self._bundles[key][1]

    async def _fetch_bundle(
        self, origin: str, destination: str, travel_date: str
    ) -> dict[str, Any]:
        # 12306 在查询前需要先建立 Cookie 会话，否则经常返回空响应。
        try:
            await self.http.get(f"{BASE_URL}/otn/leftTicket/init", headers=self._headers)
            station_map = await self._station_codes()
            rows, returned_map = await self._fetch_rows(origin, destination, travel_date)
        except (httpx.HTTPError, ValueError, json.JSONDecodeError) as error:
            raise ProviderError(
                "rail_upstream_unavailable",
                "12306 查询服务暂时不可用",
                retryable=True,
            ) from error
        if not rows:
            raise ProviderError("rail_no_result", "12306 没有返回可用车次")
        station_map.update(returned_map)
        parsed = [_parse_train_row(row, station_map) for row in rows]
        # 12306 一天可能返回数百条车次；只保留前 20 条，避免为大量无关车次
        # 并发请求票价，也让前端候选列表保持可读。
        trains = [item for item in parsed if item is not None][:MAX_RESULTS]
        if not trains:
            raise ProviderError("rail_invalid_response", "12306 返回的车次格式无法识别")
        await asyncio.gather(
            *(_attach_price(self.http, item, travel_date, self._headers) for item in trains),
            return_exceptions=True,
        )
        prices = [
            {"train_code": item["train_code"], "prices": item.get("prices", {})}
            for item in trains
            if item.get("prices")
        ]
        return {"trains": trains, "prices": prices}

    async def _fetch_rows(
        self, origin: str, destination: str, travel_date: str
    ) -> tuple[list[str], dict[str, str]]:
        params = {
            "leftTicketDTO.train_date": travel_date,
            "leftTicketDTO.from_station": origin,
            "leftTicketDTO.to_station": destination,
            "purpose_codes": "ADULT",
        }
        last_error = ""
        for endpoint in QUERY_ENDPOINTS:
            try:
                response = await self.http.get(
                    f"{BASE_URL}/otn/leftTicket/{endpoint}",
                    params=params,
                    headers=self._headers,
                )
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError, json.JSONDecodeError) as error:
                last_error = str(error)
                continue
            data = payload.get("data") or {}
            rows = data.get("result") or []
            if rows:
                return list(rows), dict(data.get("map") or {})
            last_error = str(payload.get("messages") or payload.get("message") or "无结果")
        raise ProviderError(
            "rail_query_failed", f"12306 查询失败：{last_error}", retryable=True
        )

    async def _station_codes(self) -> dict[str, str]:
        if self._stations is not None:
            return self._stations
        async with self._station_lock:
            if self._stations is not None:
                return self._stations
            response = await self.http.get(STATION_NAME_URL, headers=self._headers)
            response.raise_for_status()
            body = response.text
            payload = body.split("'", 2)[1] if "'" in body else body
            stations: dict[str, str] = {}
            for record in payload.split("@"):
                fields = record.split("|")
                if len(fields) >= 3 and fields[1] and fields[2]:
                    stations[fields[1]] = fields[2]
            if not stations:
                raise ProviderError(
                    "rail_station_list_failed", "12306 车站编码表为空", retryable=True
                )
            self._stations = stations
            return stations


async def _attach_price(
    http: httpx.AsyncClient,
    train: dict[str, Any],
    travel_date: str,
    headers: dict[str, str],
) -> None:
    """查询单个车次的真实票价；价格失败不影响余票结果。"""

    if not all(train.get(key) for key in ("train_no", "from_station_no", "to_station_no")):
        return
    params = {
        "train_no": train["train_no"],
        "from_station_no": train["from_station_no"],
        "to_station_no": train["to_station_no"],
        "seat_types": train.get("seat_types", ""),
        "train_date": travel_date,
    }
    try:
        response = await http.get(
            f"{BASE_URL}/otn/leftTicket/queryTicketPrice",
            params=params,
            headers=headers,
        )
        response.raise_for_status()
        data = response.json().get("data") or {}
    except (httpx.HTTPError, ValueError, json.JSONDecodeError):
        return
    mapping = {
        "A9": "商务座",
        "P": "特等座",
        "M": "一等座",
        "O": "二等座",
        "A6": "高级软卧",
        "A4": "软卧",
        "A3": "硬卧",
        "A1": "硬座",
        "WZ": "无座",
    }
    train["prices"] = {
        label: str(data[key]).strip()
        for key, label in mapping.items()
        if data.get(key) not in (None, "", "--", "None", "null")
    }


def _parse_train_row(raw: Any, stations: dict[str, str]) -> dict[str, Any] | None:
    if not isinstance(raw, str):
        return None
    fields = raw.split("|")
    if len(fields) < 33:
        return None
    seat_indexes = {
        "business": 32,
        "first_class": 31,
        "second_class": 30,
        "soft_sleeper": 23,
        "hard_sleeper": 28,
        "hard_seat": 29,
        "no_seat": 26,
    }
    return {
        "train_no": fields[2],
        "train_code": fields[3],
        "from_station": stations.get(fields[6], fields[6]),
        "to_station": stations.get(fields[7], fields[7]),
        "start_time": fields[8],
        "arrive_time": fields[9],
        "duration": fields[10],
        "seats": {name: fields[index].strip() for name, index in seat_indexes.items()},
        "from_station_no": fields[16] if len(fields) > 16 else "",
        "to_station_no": fields[17] if len(fields) > 17 else "",
        "seat_types": fields[35] if len(fields) > 35 else "",
    }


def _positive_int(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default
