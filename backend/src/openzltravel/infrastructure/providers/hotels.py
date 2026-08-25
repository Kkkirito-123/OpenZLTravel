"""RollingGo 酒店搜索与本地目录降级。"""

from __future__ import annotations

from typing import Any, Protocol, cast

import httpx

from openzltravel.domain.models import CandidateCatalog, HotelOption, TravelRequirements

from .base import ProviderError, ProviderRuntime, stable_fact_id, stable_key
from .geo import http_url, nonnegative_number, text
from .mcp import McpHttpClient


class HotelClient(Protocol):
    """酒店 Provider 需要的最小只读客户端接口。"""

    async def search(self, arguments: dict[str, Any]) -> Any:
        """搜索酒店列表。"""


class RollingGoHotelClient:
    """使用 RollingGo Streamable HTTP MCP 搜索酒店。

    RollingGo 的远程接口不是旧版 ``/hotelsearch`` REST 地址，而是 MCP 的
    ``tools/call(searchHotels)``。API Key 由 runtime.config 从 ``backend/.env``
    读取，Provider 本身不读取文件，也不保存密钥。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        *,
        timeout_seconds: float = 8,
        http: httpx.AsyncClient | None = None,
        client: HotelClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = (api_key or "").strip()
        self.client = client or _McpHotelClient(
            McpHttpClient(
                self.base_url,
                timeout_seconds=timeout_seconds,
                bearer_token=self.api_key,
                http=http,
            )
        )

    @property
    def authenticated(self) -> bool:
        """只判断是否配置 API Key，不在日志或响应中暴露密钥。"""

        return bool(self.api_key)

    async def search(self, arguments: dict[str, Any]) -> Any:
        """调用官方 ``searchHotels`` 工具。"""

        if not self.api_key and not isinstance(self.client, _McpHotelClient):
            # 注入 Fake Client 时允许离线测试，不需要真实 Key。
            return await self.client.search(arguments)
        if not self.api_key:
            raise ProviderError("rollinggo_api_key_missing", "RollingGo API Key 未配置")
        return await self.client.search(arguments)

    async def aclose(self) -> None:
        """关闭 MCP 客户端创建的连接池。"""

        close = getattr(self.client, "aclose", None)
        if close is not None:
            await close()


class _McpHotelClient:
    """把通用 MCP 客户端转换为 HotelClient 的命名接口。"""

    def __init__(self, client: McpHttpClient) -> None:
        self.client = client

    async def search(self, arguments: dict[str, Any]) -> Any:
        return await self.client.call_tool("searchHotels", arguments)

    async def aclose(self) -> None:
        await self.client.aclose()


class HotelProvider:
    """优先返回 RollingGo 实时候选，失败时只使用真实目录 POI。"""

    def __init__(
        self,
        client: HotelClient | None,
        *,
        timeout_seconds: float = 10,
        concurrency: int = 2,
        runtime: ProviderRuntime | None = None,
    ) -> None:
        self.client = client
        self.runtime = runtime or ProviderRuntime(
            "rollinggo", timeout_seconds=timeout_seconds, concurrency=concurrency
        )

    async def search(
        self, requirements: TravelRequirements, catalog: CandidateCatalog
    ) -> tuple[list[HotelOption], bool, str | None]:
        """搜索酒店；实时服务不可用时返回 OSM 候选和明确警告。"""

        if requirements.days_count <= 1:
            return [], False, None
        if self.client is None:
            return _local_hotels(catalog), False, "酒店实时服务未配置，当前展示本地目录候选。"
        if requirements.destination is None or requirements.start_date is None:
            raise ValueError("酒店搜索前必须确认目的地和开始日期")
        key = stable_key(
            requirements.destination,
            requirements.start_date,
            requirements.end_date,
            requirements.travelers,
            requirements.hotel_level,
        )
        try:
            payload, cache_hit = await self.runtime.run(
                key,
                lambda: cast(HotelClient, self.client).search(_search_arguments(requirements)),
                ttl_seconds=600,
            )
        except ProviderError as error:
            warning = (
                "RollingGo API Key 未配置，当前展示本地目录候选。"
                if error.code == "rollinggo_api_key_missing"
                else "酒店实时查询失败，当前展示本地目录候选。"
            )
            return _local_hotels(catalog), False, warning
        options = _sort_hotels(
            _hotel_options(payload, requirements.days_count - 1),
            requirements.hotel_level,
        )
        if options:
            return options, cache_hit, None
        return (
            _local_hotels(catalog),
            cache_hit,
            "酒店实时服务未返回结果，当前展示本地目录候选。",
        )


def _search_arguments(requirements: TravelRequirements) -> dict[str, Any]:
    return {
        "originQuery": f"{requirements.destination}酒店",
        "place": requirements.destination,
        "placeType": "城市",
        "checkInParam": {
            "adultCount": requirements.travelers,
            "checkInDate": requirements.start_date.isoformat() if requirements.start_date else "",
            "stayNights": max(1, requirements.days_count - 1),
        },
        "size": 15,
    }


def _hotel_options(payload: Any, nights: int) -> list[HotelOption]:
    return [
        option
        for item in _find_list(
            payload,
            ("hotelInformationList", "hotels", "hotelList", "list", "data"),
        )
        if (option := _hotel_option(item, nights)) is not None
    ]


def _sort_hotels(options: list[HotelOption], hotel_level: str) -> list[HotelOption]:
    """保留多档真实候选，只把住宿偏好用于稳定排序。"""

    preferred_star = {"经济": 2.5, "舒适": 3.75, "品质": 4.5}[hotel_level]
    return sorted(
        options,
        key=lambda option: (
            option.star_rating is None,
            abs((option.star_rating or preferred_star) - preferred_star),
            option.total_price is None,
            option.total_price or 0,
            option.hotel_id,
        ),
    )


def _hotel_option(item: dict[str, Any], nights: int) -> HotelOption | None:
    raw_id = text(_first(item, "hotelId", "hotel_id", "id"))
    name = text(_first(item, "hotelName", "name", "nameZh"))
    if not raw_id or not name:
        return None
    price_value = _first(item, "minPrice", "pricePerNight", "lowestPrice")
    if price_value is None and isinstance(item.get("price"), dict):
        price_value = _first(cast(dict[str, Any], item["price"]), "lowestPrice", "minPrice")
    price = nonnegative_number(price_value)
    facilities = _strings(_first(item, "hotelAmenities", "facilities"))
    tags = _strings(item.get("tags"))
    return HotelOption(
        hotel_id=stable_fact_id("hotel-rollinggo", raw_id),
        name=name,
        address=text(_first(item, "address", "addressLine")),
        latitude=_number(_first(item, "latitude", "lat")),
        longitude=_number(_first(item, "longitude", "lng", "lon")),
        star_rating=_number(_first(item, "starRating", "star")),
        price_per_night=price,
        total_price=round(price * nights, 2) if price is not None else None,
        distance_km=_distance_km(_first(item, "distanceInMeters", "distanceInMeter", "distance")),
        image_url=http_url(_first(item, "imageUrl", "image", "coverImage")),
        facilities=list(dict.fromkeys([*facilities, *tags])),
        source="rollinggo",
        booking_url=http_url(_first(item, "bookingUrl", "url")),
    )


def _local_hotels(catalog: CandidateCatalog) -> list[HotelOption]:
    return [
        HotelOption(
            hotel_id=poi.id,
            name=poi.name,
            address=poi.address,
            latitude=poi.latitude,
            longitude=poi.longitude,
            image_url=poi.image_url,
            source="osm",
        )
        for poi in catalog.hotels[:15]
    ]


def _find_list(payload: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict) and (nested := _find_list(value, keys)):
            return nested
    return []


def _first(item: dict[str, Any], *keys: str) -> Any:
    return next((item[key] for key in keys if item.get(key) is not None), None)


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        text(item.get("name") if isinstance(item, dict) else item)
        for item in value
        if text(item)
    ]


def _number(value: Any) -> float | None:
    return nonnegative_number(value)


def _distance_km(value: Any) -> float | None:
    number = nonnegative_number(value)
    if number is None:
        return None
    return round(number / 1000, 2) if number > 100 else round(number, 2)
