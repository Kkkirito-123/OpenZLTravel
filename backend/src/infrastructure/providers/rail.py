"""12306 只读铁路查询适配器。

上层只依赖 ``RailClient``，具体实现可以是直接 12306 公共接口或外部 MCP。
"""

from __future__ import annotations

import asyncio
import re
from datetime import date
from typing import Any, Literal, Protocol, cast

from domain.models import RailOption, RailSeat

from .base import ProviderError, ProviderRuntime, stable_fact_id, stable_key

Direction = Literal["outbound", "return"]

SEAT_NAMES = {
    "business": "商务座",
    "first_class": "一等座",
    "second_class": "二等座",
    "soft_sleeper": "软卧",
    "hard_sleeper": "硬卧",
    "hard_seat": "硬座",
    "no_seat": "无座",
}


class RailClient(Protocol):
    """12306 Provider 需要的最小 MCP 调用接口。"""

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """调用一个只读工具。"""


class RailProvider:
    """解析车站、余票和真实报价，缺失价格保持为空。"""

    def __init__(
        self,
        client: RailClient,
        *,
        timeout_seconds: float = 8,
        concurrency: int = 3,
        runtime: ProviderRuntime | None = None,
    ) -> None:
        self.client = client
        self.runtime = runtime or ProviderRuntime(
            "rail", timeout_seconds=timeout_seconds, concurrency=concurrency
        )

    async def search(
        self,
        origin: str,
        destination: str,
        travel_date: date,
        direction: str,
    ) -> tuple[list[RailOption], bool]:
        """并行读取余票与票价，返回稳定车次事实和缓存命中状态。"""

        if direction not in {"outbound", "return"}:
            raise ValueError("direction 必须为 outbound 或 return")
        query_origin, query_destination = (
            (destination, origin) if direction == "return" else (origin, destination)
        )
        origin_code, destination_code = await asyncio.gather(
            self._resolve_station(query_origin), self._resolve_station(query_destination)
        )
        key = stable_key(origin_code, destination_code, travel_date, direction)
        payload, cache_hit = await self.runtime.run(
            key,
            lambda: self._search_payload(origin_code, destination_code, travel_date),
            ttl_seconds=120,
        )
        return _options(payload, cast(Direction, direction), travel_date), cache_hit

    async def _resolve_station(self, value: str) -> str:
        if re.fullmatch(r"[A-Za-z]{3}", value.strip()):
            return value.strip().upper()
        normalized = value.strip()
        key = stable_key("station", normalized.casefold())
        payload, _ = await self.runtime.run(
            key,
            lambda: self.client.call_tool(
                "search-stations", {"query": normalized, "limit": 10}
            ),
            ttl_seconds=86400,
        )
        code = next(
            (
                _text(item.get("code"))
                for item in _items(payload, "stations")
                if _text(item.get("code"))
            ),
            "",
        )
        if code:
            return code
        raise ProviderError(
            "rail_station_not_found",
            _error_message(payload) or f"未找到“{value}”对应的车站",
        )

    async def _search_payload(
        self, origin_code: str, destination_code: str, travel_date: date
    ) -> dict[str, Any]:
        arguments = {
            "from_station": origin_code,
            "to_station": destination_code,
            "train_date": travel_date.isoformat(),
        }
        results: list[Any] = list(
            await asyncio.gather(
                self.client.call_tool("query-tickets", arguments),
                self.client.call_tool("query-ticket-price", arguments),
                return_exceptions=True,
            )
        )
        tickets, prices = results
        if isinstance(tickets, BaseException):
            raise tickets
        _raise_business_failure(tickets)
        # 票价是可选事实；票价接口失败不能把可用余票一起丢掉。
        return {"tickets": tickets, "prices": {} if isinstance(prices, BaseException) else prices}


def _options(payload: Any, direction: Direction, travel_date: date) -> list[RailOption]:
    data = payload if isinstance(payload, dict) else {}
    tickets = _items(data.get("tickets"), "trains")
    prices = {_train_code(item): item for item in _items(data.get("prices"), "data")}
    return [
        _option(item, prices.get(_train_code(item), {}), direction, travel_date)
        for item in tickets
        if _train_code(item)
    ]


def _option(
    train: dict[str, Any],
    price: dict[str, Any],
    direction: Direction,
    travel_date: date,
) -> RailOption:
    code = _train_code(train)
    price_map = price.get("prices") if isinstance(price.get("prices"), dict) else {}
    seats = _seats(train.get("seats"), cast(dict[str, Any], price_map))
    return RailOption(
        option_id=stable_fact_id(
            "rail",
            direction,
            travel_date,
            code,
            _text(train.get("from_station")),
            _text(train.get("to_station")),
        ),
        direction=direction,
        travel_date=travel_date,
        train_code=code,
        from_station=_text(train.get("from_station")),
        to_station=_text(train.get("to_station")),
        departure_time=_text(train.get("start_time")),
        arrival_time=_text(train.get("arrive_time")),
        duration_minutes=_duration(train.get("duration")),
        seats=seats,
        price_from=min((seat.price for seat in seats if seat.price is not None), default=None),
        has_ticket=any(_available(seat.availability) for seat in seats),
    )


def _seats(value: Any, prices: dict[str, Any]) -> list[RailSeat]:
    seats = value if isinstance(value, dict) else {}
    results: list[RailSeat] = []
    for key, availability in seats.items():
        if _text(availability) in {"", "--"}:
            continue
        name = SEAT_NAMES.get(str(key), str(key))
        results.append(
            RailSeat(
                name=name,
                availability=_text(availability) or "未知",
                price=_price(prices.get(name) or prices.get(key)),
            )
        )
    return results


def _items(payload: Any, key: str) -> list[dict[str, Any]]:
    values = payload.get(key, []) if isinstance(payload, dict) else []
    return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []


def _train_code(item: dict[str, Any]) -> str:
    return _text(item.get("train_code") or item.get("train_no"))


def _duration(value: Any) -> int:
    parts = _text(value).split(":")
    try:
        return int(parts[0]) * 60 + int(parts[1]) if len(parts) >= 2 else 0
    except ValueError:
        return 0


def _price(value: Any) -> float | None:
    try:
        return float(str(value).replace("¥", "").replace("￥", ""))
    except (TypeError, ValueError):
        return None


def _available(value: str) -> bool:
    return value not in {"无", "0", "候补", "未知", "--", ""}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _raise_business_failure(payload: Any) -> None:
    if isinstance(payload, dict) and payload.get("success", True) is False:
        raise ProviderError(
            "rail_query_failed", _error_message(payload) or "12306 暂无可用车次"
        )


def _error_message(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, str):
        return error.strip()
    errors = payload.get("errors")
    if isinstance(errors, list):
        return "；".join(_text(item) for item in errors if _text(item))
    return ""
