"""OpenZLTravel 的外部数据与模型提供者。

本文件集中定义地图、天气和规划器接口，并提供高德与 OpenAI 兼容实现。
供应商响应只在这里转换为领域模型，不承担行程编排或持久化职责。
"""

import json
from datetime import date
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

import httpx
from openai import OpenAI
from pydantic import ValidationError

from app.config import Settings
from app.errors import DraftError, ProviderError
from app.models import (
    CandidateCatalog,
    City,
    Coordinate,
    ItineraryDraft,
    Poi,
    RouteSegment,
    TravelRequest,
    WeatherDay,
)


class MapProvider(Protocol):
    """旅行服务依赖的地图与天气能力。"""

    def resolve_city(self, destination: str) -> City:
        """确认目的地城市。"""

        ...

    def search_candidates(self, city: City) -> CandidateCatalog:
        """返回城市内可供模型选择的真实 POI。"""

        ...

    def get_weather(
        self,
        city: City,
        start_date: date,
        end_date: date,
    ) -> list[WeatherDay]:
        """返回日期范围内供应商实际提供的天气。"""

        ...

    def get_route(self, from_poi: Poi, to_poi: Poi) -> RouteSegment:
        """返回两个真实 POI 之间的路线。"""

        ...


class Planner(Protocol):
    """结构化行程规划器接口。"""

    def plan(
        self,
        request: TravelRequest,
        candidates: CandidateCatalog,
        feedback: str | None = None,
    ) -> ItineraryDraft:
        """从候选池生成结构化行程草稿。"""

        ...


class AmapClient:
    """高德 Web 服务 API 的最小封装。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.http = httpx.Client(
            base_url=settings.amap_base_url,
            timeout=settings.amap_timeout_seconds,
        )

    def _get(self, path: str, **params: str) -> dict[str, Any]:
        if not self.settings.amap_api_key:
            raise ProviderError("amap_not_configured", "尚未配置高德地图 API Key")
        try:
            response = self.http.get(path, params={**params, "key": self.settings.amap_api_key})
            response.raise_for_status()
            payload = cast(dict[str, Any], response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError("amap_unavailable", "高德地图服务暂时不可用") from exc
        if payload.get("status") != "1":
            raise ProviderError("amap_request_failed", payload.get("info", "高德请求失败"))
        return payload

    def resolve_city(self, destination: str) -> City:
        """通过地理编码确认目的地城市和行政区编码。"""

        payload = self._get("/geocode/geo", address=destination)
        geocode = _first(payload.get("geocodes"))
        if not geocode:
            raise ProviderError("city_not_found", f"无法确认目的地“{destination}”")
        name = _text(geocode.get("city")) or _text(geocode.get("district")) or destination
        latitude, longitude = _location(geocode.get("location"))
        return City(
            name=name,
            adcode=geocode.get("adcode"),
            latitude=latitude,
            longitude=longitude,
        )

    def search_candidates(self, city: City) -> CandidateCatalog:
        """分别查询景点、餐厅和酒店，统一转成候选池。"""

        return CandidateCatalog(
            attractions=self._search(city, "风景名胜", "attraction"),
            restaurants=self._search(city, "餐饮服务", "restaurant"),
            hotels=self._search(city, "住宿服务", "hotel"),
        )

    def _search(self, city: City, keyword: str, category: str) -> list[Poi]:
        payload = self._get(
            "/place/text",
            keywords=keyword,
            city=city.adcode or city.name,
            citylimit="true",
            offset="8",
            extensions="all",
        )
        return [
            poi for raw in payload.get("pois", []) if (poi := _parse_poi(raw, category)) is not None
        ]

    def get_weather(self, city: City, start_date: date, end_date: date) -> list[WeatherDay]:
        """获取高德可提供的天气预报，超出范围的日期由应用层补充警告。"""

        payload = self._get(
            "/weather/weatherInfo",
            city=city.adcode or city.name,
            extensions="all",
        )
        forecasts = _first(payload.get("forecasts")) or {}
        return [
            WeatherDay(
                date=date.fromisoformat(item["date"]),
                day_weather=item.get("dayweather"),
                night_weather=item.get("nightweather"),
                day_temperature=item.get("daytemp"),
                night_temperature=item.get("nighttemp"),
            )
            for item in forecasts.get("casts", [])
            if _in_range(item.get("date"), start_date, end_date)
        ]

    def get_route(self, from_poi: Poi, to_poi: Poi) -> RouteSegment:
        """查询两个已知 POI 的驾车路线。"""

        payload = self._get(
            "/direction/driving",
            origin=f"{from_poi.longitude},{from_poi.latitude}",
            destination=f"{to_poi.longitude},{to_poi.latitude}",
            extensions="all",
        )
        path = _first(payload.get("route", {}).get("paths"))
        if path is None:
            raise ProviderError("route_not_found", "无法获取两个景点之间的驾车路线")
        polyline = [
            coordinate
            for step in path.get("steps", [])
            for coordinate in _polyline(step.get("polyline", ""))
        ]
        return RouteSegment(
            from_poi_id=from_poi.id,
            to_poi_id=to_poi.id,
            distance_km=round(float(path.get("distance", 0)) / 1000, 2),
            duration_minutes=max(1, round(float(path.get("duration", 0)) / 60)),
            polyline=polyline,
        )


def _parse_poi(raw: dict[str, Any], category: str) -> Poi | None:
    latitude, longitude = _location(raw.get("location"))
    if latitude is None or longitude is None or not raw.get("id") or not raw.get("name"):
        return None
    return Poi(
        id=raw["id"],
        name=raw["name"],
        address=_text(raw.get("address")),
        category=category,  # type: ignore[arg-type]
        latitude=latitude,
        longitude=longitude,
        type_name=_text(raw.get("type")),
        image_url=_photo_url(raw.get("photos")),
    )


def _location(value: Any) -> tuple[float | None, float | None]:
    if not isinstance(value, str) or "," not in value:
        return None, None
    longitude_text, latitude_text = value.split(",", 1)
    try:
        return float(latitude_text), float(longitude_text)
    except ValueError:
        return None, None


def _polyline(value: str) -> list[Coordinate]:
    points: list[Coordinate] = []
    for item in value.split(";"):
        latitude, longitude = _location(item)
        if latitude is not None and longitude is not None:
            points.append(Coordinate(latitude=latitude, longitude=longitude))
    return points


def _first(value: Any) -> dict[str, Any] | None:
    return value[0] if isinstance(value, list) and value else None


def _text(value: Any) -> str:
    if isinstance(value, list):
        return "".join(str(item) for item in value)
    return str(value or "")


def _photo_url(value: Any) -> str | None:
    """只接受高德照片列表中的 HTTP(S) 地址，不下载第三方图片。"""

    if not isinstance(value, list):
        return None
    for photo in value:
        url = photo.get("url") if isinstance(photo, dict) else None
        parsed = urlsplit(url) if isinstance(url, str) else None
        if parsed and parsed.scheme in {"http", "https"} and parsed.netloc:
            return url
    return None


def _in_range(value: Any, start_date: date, end_date: date) -> bool:
    try:
        item_date = date.fromisoformat(str(value))
    except ValueError:
        return False
    return start_date <= item_date <= end_date


# ============================================================================
# OpenAI 兼容模型规划器
# ============================================================================


class LlmPlanner:
    """让模型做有限的结构化选择，不允许模型直接编造地图事实。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = OpenAI(
            api_key=settings.llm_api_key or "missing-key",
            base_url=settings.llm_base_url or None,
            timeout=settings.llm_timeout_seconds,
        )

    def plan(
        self,
        request: TravelRequest,
        candidates: CandidateCatalog,
        feedback: str | None = None,
    ) -> ItineraryDraft:
        """请求一次结构化规划，解析失败由旅行服务决定是否修复重试。"""

        if not self.settings.llm_api_key or not self.settings.llm_model:
            raise ProviderError(
                "llm_not_configured",
                "尚未配置兼容模型的 API Key 或模型名",
            )
        try:
            response = self.client.chat.completions.create(
                model=self.settings.llm_model,
                temperature=0.2,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": _system_prompt()},
                    {
                        "role": "user",
                        "content": _user_prompt(request, candidates, feedback),
                    },
                ],
            )
            content = response.choices[0].message.content or ""
            return ItineraryDraft.model_validate(json.loads(_clean_json(content)))
        except (ValidationError, json.JSONDecodeError, IndexError, TypeError) as exc:
            raise DraftError("模型返回的行程结构无法解析，请重试") from exc
        except Exception as exc:
            raise ProviderError("llm_unavailable", "规划模型暂时不可用") from exc


def _system_prompt() -> str:
    """返回约束模型只能选择真实候选地点的系统提示词。"""

    return """你是一个严谨的旅行规划器。只输出 JSON，不输出 Markdown。
你只能使用用户提供的候选 POI 的 id；不能创造地点、地址、坐标、天气或路线。
每天安排 1 到 4 个景点，餐厅和酒店只能从对应候选池选择。"""


def _user_prompt(
    request: TravelRequest,
    candidates: CandidateCatalog,
    feedback: str | None,
) -> str:
    payload: dict[str, Any] = {
        "request": request.model_dump(mode="json"),
        "candidates": candidates.prompt_data(),
        "output_schema": {
            "summary": "string",
            "days": [
                {
                    "day_index": 1,
                    "theme": "string",
                    "activities": [
                        {
                            "poi_id": "候选景点 id",
                            "start_time": "09:00",
                            "duration_minutes": 120,
                            "note": "string",
                        }
                    ],
                    "meal_ids": ["候选餐厅 id"],
                    "hotel_id": "候选酒店 id 或 null",
                    "notes": ["string"],
                }
            ],
            "tips": ["string"],
        },
    }
    if feedback:
        payload["previous_error"] = feedback
    return json.dumps(payload, ensure_ascii=False)


def _clean_json(content: str) -> str:
    """兼容少数模型仍返回 JSON 代码围栏的情况。"""

    return content.strip().removeprefix("```json").removesuffix("```").strip()
