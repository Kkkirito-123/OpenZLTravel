"""12306 MCP 的只读铁路查询适配器。

工具响应在本文件转换为稳定模型。网络失败只影响对应步骤，是否降级由规划运行时决定；
票价缺失时保持空值，绝不使用模型猜测。
"""

import asyncio
import hashlib
import re
from datetime import date
from typing import Any

from app.errors import ProviderError
from app.models import RailOption, RailSeat, RailSegment
from app.providers.base import McpHttpClient, ProviderExecutor, stable_key

SEAT_NAMES = {
    "business": "商务座",
    "first_class": "一等座",
    "second_class": "二等座",
    "soft_sleeper": "软卧",
    "hard_sleeper": "硬卧",
    "hard_seat": "硬座",
    "no_seat": "无座",
}


class RailProvider:
    """查询直达车、票价和一次中转方案。"""

    def __init__(self, client: McpHttpClient, executor: ProviderExecutor) -> None:
        self.client = client
        self.executor = executor

    async def search(
        self, origin: str, destination: str, travel_date: date, direction: str
    ) -> tuple[list[RailOption], bool]:
        """并行查询余票和票价，再按车次合并。"""

        key = stable_key(origin, destination, travel_date, direction, "direct")
        payload, cache_hit = await self.executor.run(
            key,
            120,
            lambda: self._search_payload(origin, destination, travel_date),
        )
        return _direct_options(payload, direction, travel_date), cache_hit

    async def transfers(
        self, origin: str, destination: str, travel_date: date, direction: str
    ) -> tuple[list[RailOption], bool]:
        """仅在无直达或用户主动展开时查询一次中转。"""

        key = stable_key(origin, destination, travel_date, direction, "transfer")
        payload, cache_hit = await self.executor.run(
            key,
            120,
            lambda: self._transfer_payload(origin, destination, travel_date),
        )
        return _transfer_options(payload, direction, travel_date), cache_hit

    async def quote_transfer(self, option: RailOption) -> RailOption:
        """用户选中中转方案后再查询各段票价，避免首次发现放大 12306 请求量。"""

        if not option.is_transfer or not option.segments:
            return option
        key = stable_key(option.option_id, "transfer-prices")
        payloads, _ = await self.executor.run(
            key,
            120,
            lambda: self._transfer_price_payloads(option),
        )
        return _quoted_transfer(option, payloads)

    async def _search_payload(
        self, origin: str, destination: str, travel_date: date
    ) -> dict[str, Any]:
        origin_code, destination_code = await asyncio.gather(
            self._resolve_station(origin), self._resolve_station(destination)
        )
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
        _raise_on_business_failure(tickets)
        return {
            "tickets": tickets,
            "prices": {} if isinstance(prices, BaseException) else prices,
        }

    async def _resolve_station(self, value: str) -> str:
        """把城市或车站名称解析为 12306 三字码，避免城市名直接查询失败。"""

        if _is_station_code(value):
            return value.upper()
        key = stable_key("station", value.strip().casefold())
        payload, _ = await self.executor.run(
            key,
            86400,
            lambda: self.client.call_tool("search-stations", {"query": value.strip(), "limit": 10}),
        )
        stations = _items(payload, "stations")
        code = next((_text(item.get("code")) for item in stations if _text(item.get("code"))), "")
        if code:
            return code
        message = _error_message(payload) or f"未找到“{value}”对应的车站"
        raise ProviderError("rail_station_not_found", message)

    async def _transfer_price_payloads(self, option: RailOption) -> list[Any]:
        operations = [
            self.client.call_tool(
                "query-ticket-price",
                {
                    "from_station": segment.from_station,
                    "to_station": segment.to_station,
                    "train_date": option.travel_date.isoformat(),
                    "train_code": segment.train_code,
                },
            )
            for segment in option.segments
        ]
        return list(await asyncio.gather(*operations, return_exceptions=True))

    async def _transfer_payload(self, origin: str, destination: str, travel_date: date) -> Any:
        origin_code, destination_code = await asyncio.gather(
            self._resolve_station(origin), self._resolve_station(destination)
        )
        return await self.client.call_tool(
            "query-transfer",
            {
                "from_station": origin_code,
                "to_station": destination_code,
                "train_date": travel_date.isoformat(),
                "middle_station": "",
                "isShowWZ": "N",
                "purpose_codes": "00",
            },
        )


def _direct_options(payload: Any, direction: str, travel_date: date) -> list[RailOption]:
    data = payload if isinstance(payload, dict) else {}
    tickets = _items(data.get("tickets"), "trains")
    prices = {_train_code(item): item for item in _items(data.get("prices"), "data")}
    return [
        _direct_option(item, prices.get(_train_code(item), {}), direction, travel_date)
        for item in tickets
        if _train_code(item)
    ]


def _direct_option(
    train: dict[str, Any], price: dict[str, Any], direction: str, travel_date: date
) -> RailOption:
    code = _train_code(train)
    price_map = price.get("prices", {}) if isinstance(price.get("prices"), dict) else {}
    seats = _seats(train.get("seats"), price_map)
    return RailOption(
        option_id=_option_id(direction, travel_date, code),
        direction=direction,  # type: ignore[arg-type]
        travel_date=travel_date,
        train_code=code,
        train_type=_train_type(code, price.get("train_class_name")),
        from_station=_text(train.get("from_station")),
        to_station=_text(train.get("to_station")),
        departure_time=_text(train.get("start_time")),
        arrival_time=_text(train.get("arrive_time")),
        duration_minutes=_duration(train.get("duration")),
        seats=seats,
        price_from=min((item.price for item in seats if item.price is not None), default=None),
        has_ticket=any(_available(item.availability) for item in seats),
    )


def _transfer_options(payload: Any, direction: str, travel_date: date) -> list[RailOption]:
    options: list[RailOption] = []
    for index, item in enumerate(_items(payload, "transfers")):
        option = _transfer_option(item, direction, travel_date, index)
        if option is not None:
            options.append(option)
    return options


def _transfer_option(
    item: dict[str, Any], direction: str, travel_date: date, index: int
) -> RailOption | None:
    segments = [_segment(value) for value in _items(item, "segments")]
    if not segments:
        return None
    first, last = segments[0], segments[-1]
    code = " + ".join(segment.train_code for segment in segments)
    seats = _combined_transfer_seats(segments)
    return RailOption(
        option_id=_option_id(direction, travel_date, f"transfer-{index}-{code}"),
        direction=direction,  # type: ignore[arg-type]
        travel_date=travel_date,
        train_code=code,
        train_type="中转",
        from_station=first.from_station,
        to_station=last.to_station,
        departure_time=first.departure_time,
        arrival_time=last.arrival_time,
        duration_minutes=_duration(item.get("total_duration")),
        seats=seats,
        has_ticket=any(_available(seat.availability) for seat in seats),
        is_transfer=True,
        transfer_station=_text(item.get("middle_station")) or None,
        segments=segments,
    )


def _quoted_transfer(option: RailOption, payloads: list[Any]) -> RailOption:
    """把各段真实报价写回中转方案；缺失任一段时总价保持未知。"""

    segments = [
        _quote_segment(segment, payloads[index] if index < len(payloads) else None)
        for index, segment in enumerate(option.segments)
    ]
    seats = _combined_transfer_seats(segments)
    return option.model_copy(
        update={
            "segments": segments,
            "seats": seats,
            "price_from": min(
                (seat.price for seat in seats if seat.price is not None),
                default=None,
            ),
        }
    )


def _quote_segment(segment: RailSegment, payload: Any) -> RailSegment:
    if isinstance(payload, BaseException):
        return segment
    prices: dict[str, Any] = next(
        (
            item.get("prices", {})
            for item in _items(payload, "data")
            if _train_code(item) == segment.train_code
        ),
        {},
    )
    if not isinstance(prices, dict):
        return segment
    seats = [
        seat.model_copy(update={"price": _price(prices.get(seat.name))}) for seat in segment.seats
    ]
    return segment.model_copy(update={"seats": seats})


def _combined_transfer_seats(segments: list[RailSegment]) -> list[RailSeat]:
    """仅聚合每一段都存在的席别，价格为各段报价之和。"""

    if not segments:
        return []
    common_names = set(seat.name for seat in segments[0].seats)
    for segment in segments[1:]:
        common_names &= {seat.name for seat in segment.seats}
    return [_combined_seat(name, segments) for name in sorted(common_names)]


def _combined_seat(name: str, segments: list[RailSegment]) -> RailSeat:
    values = [next(seat for seat in segment.seats if seat.name == name) for segment in segments]
    prices = [seat.price for seat in values]
    price = (
        round(sum(item for item in prices if item is not None), 2)
        if all(item is not None for item in prices)
        else None
    )
    available = all(_available(seat.availability) for seat in values)
    return RailSeat(name=name, availability="有" if available else "余票不足", price=price)


def _segment(item: dict[str, Any]) -> RailSegment:
    return RailSegment(
        train_code=_train_code(item),
        from_station=_text(item.get("from_station")),
        to_station=_text(item.get("to_station")),
        departure_time=_text(item.get("start_time")),
        arrival_time=_text(item.get("arrive_time")),
        duration_minutes=_duration(item.get("duration")),
        seats=_seats(item.get("seats"), {}),
    )


def _seats(value: Any, prices: dict[str, Any]) -> list[RailSeat]:
    seats = value if isinstance(value, dict) else {}
    return [
        RailSeat(
            name=SEAT_NAMES.get(str(key), str(key)),
            availability=_text(availability) or "未知",
            price=_price(prices.get(SEAT_NAMES.get(str(key), str(key)))),
        )
        for key, availability in seats.items()
        if _text(availability) not in {"", "--"}
    ]


def _items(payload: Any, key: str) -> list[dict[str, Any]]:
    values = payload.get(key, []) if isinstance(payload, dict) else []
    if not isinstance(values, list):
        return []
    return [item for item in values if isinstance(item, dict)]


def _train_code(item: dict[str, Any]) -> str:
    return _text(item.get("train_code") or item.get("train_no"))


def _train_type(code: str, value: Any) -> str:
    if _text(value):
        return _text(value)
    labels = {"G": "高铁", "D": "动车", "C": "城际", "Z": "直达", "T": "特快", "K": "快速"}
    return labels.get(code[:1], "列车")


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


def _option_id(direction: str, travel_date: date, code: str) -> str:
    raw = f"{direction}:{travel_date.isoformat()}:{code}".encode()
    return hashlib.sha1(raw).hexdigest()[:16]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _is_station_code(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z]{3}", value.strip()))


def _raise_on_business_failure(payload: Any) -> None:
    if not isinstance(payload, dict) or payload.get("success", True) is not False:
        return
    raise ProviderError("rail_query_failed", _error_message(payload) or "12306 暂无可用车次")


def _error_message(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        return error.strip()
    errors = payload.get("errors")
    if isinstance(errors, list):
        messages = [_text(item) for item in errors if _text(item)]
        return "；".join(messages)
    return ""
