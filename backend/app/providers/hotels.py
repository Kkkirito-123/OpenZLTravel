"""RollingGo 酒店服务、兼容 DIDA MCP 与本地 OSM 降级适配器。

首次发现只查询酒店列表；房型和退改规则在用户打开详情时才加载。实时服务未登录、
未配置或失败时使用本地酒店候选，保证住宿步骤不会阻断整个旅行规划。
"""

import json
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx

from app.errors import ProviderError
from app.models import CandidateCatalog, HotelDetail, HotelOption, HotelRoom, PlanningRequest
from app.providers.base import McpHttpClient, ProviderExecutor, stable_key

RealtimeHotelSource = Literal["rollinggo", "dida"]


class HotelClient(Protocol):
    """酒店供应商客户端需要满足的最小调用接口。"""

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """调用酒店查询能力并返回可解析数据。"""

        ...


class RollingGoHotelClient:
    """使用 RollingGo Skill OAuth 令牌访问只读酒店查询接口。"""

    ENDPOINTS = {
        "searchHotels": "/hotelsearch",
        "getHotelDetail": "/hoteldetail",
    }

    def __init__(
        self,
        base_url: str,
        token_path: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token_path = Path(token_path).expanduser()
        self.http = httpx.AsyncClient(timeout=timeout_seconds, transport=transport)

    @property
    def authenticated(self) -> bool:
        """仅判断 OAuth 令牌文件是否存在，不读取或暴露令牌内容。"""

        return self.token_path.is_file()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """将项目内稳定工具名映射到 RollingGo 酒店 HTTP 接口。"""

        endpoint = self.ENDPOINTS.get(name)
        if endpoint is None:
            raise ProviderError("rollinggo_tool_unsupported", "酒店服务不支持该查询")
        response = await self.http.post(
            f"{self.base_url}{endpoint}",
            headers={
                "Authorization": f"Bearer {self._access_token()}",
                "Accept": "application/json",
            },
            json=arguments,
        )
        response.raise_for_status()
        try:
            return response.json()
        except ValueError as error:
            raise ProviderError(
                "rollinggo_invalid_response", "酒店服务返回了无法识别的数据"
            ) from error

    async def aclose(self) -> None:
        """关闭复用的 HTTP 连接池。"""

        await self.http.aclose()

    def _access_token(self) -> str:
        try:
            payload = json.loads(self.token_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ProviderError(
                "rollinggo_login_required", "RollingGo 酒店服务需要先完成登录"
            ) from error
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise ProviderError("rollinggo_login_required", "RollingGo 酒店服务需要先完成登录")
        return token.strip()


class HotelProvider:
    """搜索酒店并按需加载详情。"""

    def __init__(
        self,
        client: HotelClient | McpHttpClient | None,
        executor: ProviderExecutor,
        source: RealtimeHotelSource = "dida",
    ) -> None:
        self.client = client
        self.executor = executor
        self.source = source

    async def search(
        self, request: PlanningRequest, catalog: CandidateCatalog
    ) -> tuple[list[HotelOption], bool, str | None]:
        """优先查询实时酒店；未配置或失败由 OSM 目录兜底。"""

        if self.client is None:
            return _local_hotels(catalog), False, "酒店实时服务未配置，当前展示本地候选。"
        client = self.client
        key = stable_key(
            request.destination,
            request.start_date,
            request.end_date,
            request.travelers,
            request.hotel_level,
        )
        try:
            payload, cache_hit = await self.executor.run(
                key,
                600,
                lambda: client.call_tool("searchHotels", _search_arguments(request)),
            )
        except ProviderError as error:
            warning = (
                "RollingGo 酒店服务未登录，当前展示本地候选。"
                if error.code == "rollinggo_login_required"
                else "酒店实时查询失败，当前展示本地候选。"
            )
            return _local_hotels(catalog), False, warning
        options = _hotel_options(payload, request, self.source)
        if options:
            return options, cache_hit, None
        return _local_hotels(catalog), cache_hit, "酒店实时服务未返回结果，当前展示本地候选。"

    async def detail(
        self, hotel: HotelOption, request: PlanningRequest
    ) -> tuple[HotelDetail, bool]:
        """详情按点击懒加载；本地酒店直接返回已有静态事实。"""

        if self.client is None or hotel.source == "osm":
            return _local_detail(hotel), False
        client = self.client
        key = stable_key(hotel.hotel_id, request.start_date, request.end_date, request.travelers)
        payload, cache_hit = await self.executor.run(
            key,
            300,
            lambda: client.call_tool("getHotelDetail", _detail_arguments(hotel, request)),
        )
        return _hotel_detail(payload, hotel, self.source), cache_hit


def _search_arguments(request: PlanningRequest) -> dict[str, Any]:
    stars = {"经济": [0.0, 3.0], "舒适": [3.0, 4.5], "品质": [4.0, 5.0]}
    nights = max(1, (request.end_date - request.start_date).days)
    arguments: dict[str, Any] = {
        "originQuery": f"{request.destination}{request.hotel_level}酒店",
        "place": request.destination,
        "placeType": "城市",
        "checkInParam": {
            "adultCount": request.travelers,
            "checkInDate": request.start_date.isoformat(),
            "stayNights": nights,
        },
        "filterOptions": {"starRatings": stars[request.hotel_level]},
        "size": 10,
    }
    if request.budget:
        arguments["hotelTags"] = {"maxPricePerNight": request.budget / nights}
    return arguments


def _detail_arguments(hotel: HotelOption, request: PlanningRequest) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "dateParam": {
            "checkInDate": request.start_date.isoformat(),
            "checkOutDate": request.end_date.isoformat(),
        },
        "occupancyParam": {"adultCount": request.travelers, "roomCount": 1},
        "localeParam": {"countryCode": "CN", "currency": "CNY"},
    }
    try:
        arguments["hotelId"] = int(hotel.hotel_id)
    except ValueError:
        arguments["name"] = hotel.name
    return arguments


def _hotel_options(
    payload: Any, request: PlanningRequest, source: RealtimeHotelSource
) -> list[HotelOption]:
    values = _find_list(payload, ("hotelInformationList", "hotels", "hotelList", "list", "data"))
    nights = max(1, (request.end_date - request.start_date).days)
    return [
        option for item in values if (option := _hotel_option(item, nights, source)) is not None
    ]


def _hotel_option(
    item: dict[str, Any], nights: int, source: RealtimeHotelSource
) -> HotelOption | None:
    hotel_id = _text(_first_value(item, "hotelId", "hotel_id", "id"))
    name = _text(_first_value(item, "hotelName", "name", "nameZh"))
    if not hotel_id or not name:
        return None
    price_value = _first_value(item, "minPrice", "pricePerNight", "lowestPrice")
    price_data = item.get("price")
    if price_value is None and isinstance(price_data, dict):
        price_value = _first_value(price_data, "lowestPrice", "minPrice")
    price = _number(price_value)
    facilities = _strings(_first_value(item, "hotelAmenities", "facilities"))
    tags = _strings(item.get("tags"))
    return HotelOption(
        hotel_id=hotel_id,
        name=name,
        address=_text(_first_value(item, "address", "addressLine")),
        latitude=_number(_first_value(item, "latitude", "lat")),
        longitude=_number(_first_value(item, "longitude", "lng", "lon")),
        star_rating=_number(_first_value(item, "starRating", "star")),
        price_per_night=price,
        total_price=round(price * nights, 2) if price is not None else None,
        distance_km=_distance_km(
            _first_value(item, "distanceInMeters", "distanceInMeter", "distance")
        ),
        image_url=_url(_first_value(item, "imageUrl", "image", "coverImage")),
        facilities=list(dict.fromkeys([*facilities, *tags])),
        source=source,
        booking_url=_url(_first_value(item, "bookingUrl", "url")),
    )


def _hotel_detail(payload: Any, fallback: HotelOption, source: RealtimeHotelSource) -> HotelDetail:
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    data = data if isinstance(data, dict) else {}
    if data.get("success") is False:
        raise ProviderError("hotel_detail_unavailable", "酒店房型暂时不可用")
    rooms = _find_list(data, ("roomRatePlans", "rooms", "roomList", "rates", "roomTypes"))
    images = [
        url for value in _strings(_first_value(data, "images", "imageList")) if (url := _url(value))
    ]
    return HotelDetail(
        hotel_id=fallback.hotel_id,
        name=_text(_first_value(data, "hotelName", "name")) or fallback.name,
        address=_text(_first_value(data, "address")) or fallback.address,
        description=_text(_first_value(data, "description", "introduction")),
        facilities=_strings(_first_value(data, "facilities", "tags")) or fallback.facilities,
        images=images or ([fallback.image_url] if fallback.image_url else []),
        rooms=[_room(item, index) for index, item in enumerate(rooms)],
        booking_url=_url(_first_value(data, "bookingUrl", "url")) or fallback.booking_url,
        source=source,
    )


def _room(item: dict[str, Any], index: int) -> HotelRoom:
    inventory = _number(item.get("inventoryCount"))
    return HotelRoom(
        room_id=_text(_first_value(item, "ratePlanId", "roomTypeId", "roomId", "id"))
        or f"room-{index}",
        name=_text(_first_value(item, "roomNameCn", "roomName", "name")) or "标准房型",
        price=_number(_first_value(item, "price", "totalPrice", "retailPrice")),
        breakfast=_text(_first_value(item, "breakfast", "breakfastInfo", "ratePlanName")) or None,
        cancellation=_cancellation(item),
        available=bool(item.get("available", True)) and (inventory is None or inventory > 0),
    )


def _cancellation(item: dict[str, Any]) -> str | None:
    direct = _text(_first_value(item, "cancellation", "cancelPolicy"))
    if direct:
        return direct
    policies = item.get("cancellationPolicies")
    if not isinstance(policies, list):
        return None
    descriptions = [
        _text(policy.get("description"))
        for policy in policies
        if isinstance(policy, dict) and _text(policy.get("description"))
    ]
    return "；".join(descriptions) or None


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
        for poi in catalog.hotels[:10]
    ]


def _local_detail(hotel: HotelOption) -> HotelDetail:
    return HotelDetail(
        hotel_id=hotel.hotel_id,
        name=hotel.name,
        address=hotel.address,
        facilities=hotel.facilities,
        images=[hotel.image_url] if hotel.image_url else [],
        source="osm",
    )


def _find_list(payload: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _find_list(value, keys)
            if nested:
                return nested
    return []


def _first_value(item: dict[str, Any], *keys: str) -> Any:
    return next((item[key] for key in keys if item.get(key) is not None), None)


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        _text(item.get("name") if isinstance(item, dict) else item) for item in value if _text(item)
    ]


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _distance_km(value: Any) -> float | None:
    number = _number(value)
    if number is None:
        return None
    return round(number / 1000, 2) if number > 100 else round(number, 2)


def _url(value: Any) -> str | None:
    if isinstance(value, list):
        return next((_url(item) for item in value if _url(item)), None)
    if isinstance(value, dict):
        return _url(_first_value(value, "url", "imageUrl"))
    parsed = urlsplit(value) if isinstance(value, str) else None
    return value if parsed and parsed.scheme in {"http", "https"} and parsed.netloc else None


def _text(value: Any) -> str:
    return str(value or "").strip()
