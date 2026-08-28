"""Open-Meteo 主来源与高德天气兜底。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

import httpx

from domain.models import City, WeatherDay

from .base import ProviderError, ProviderRuntime, stable_key
from .geo import list_values

WEATHER_FORECAST_DAYS = 16
FUTURE_FORECAST_WARNING = "距离出行日期较远，尚未进入可靠天气预报覆盖期"
WEATHER_SERVICE_WARNING = "天气服务暂时不可用"


class WeatherFallback(Protocol):
    """天气兜底需要的最小异步接口。"""

    async def get_weather(
        self, city: City, start_date: date, end_date: date
    ) -> list[WeatherDay]:
        """返回供应商实际覆盖的预报。"""


class OpenMeteoClient:
    """异步查询 Open-Meteo 并把 WMO 代码转为中文天气事实。"""

    def __init__(
        self,
        *,
        base_url: str = "https://api.open-meteo.com/v1/forecast",
        timeout_seconds: float = 5,
        concurrency: int = 3,
        runtime: ProviderRuntime | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.runtime = runtime or ProviderRuntime(
            "open_meteo", timeout_seconds=timeout_seconds, concurrency=concurrency
        )
        self.http = http or httpx.AsyncClient(timeout=timeout_seconds)
        self.base_url = base_url
        self._owns_http = http is None

    async def get_weather(
        self, city: City, start_date: date, end_date: date
    ) -> list[WeatherDay]:
        """读取日最高/最低温和 WMO 天气代码。"""

        if city.latitude is None or city.longitude is None:
            raise ProviderError("weather_unavailable", "目的地缺少天气查询坐标")
        key = stable_key(
            round(city.latitude, 5), round(city.longitude, 5), start_date, end_date
        )

        async def request() -> dict[str, Any]:
            """执行一次 Open-Meteo 请求，重试、并发限制和缓存统一交给 ProviderRuntime。"""

            response = await self.http.get(
                self.base_url,
                params={
                    "latitude": city.latitude,
                    "longitude": city.longitude,
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                    "timezone": "Asia/Shanghai",
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                },
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("open-meteo response must be object")
            return cast(dict[str, Any], payload)

        payload, _ = await self.runtime.run(key, request, ttl_seconds=1800)
        return _parse_weather(payload)

    async def aclose(self) -> None:
        """关闭本实例创建的 HTTP 连接池。"""

        if self._owns_http:
            await self.http.aclose()


class WeatherProvider:
    """优先使用 Open-Meteo，再用高德补齐；远期和服务失败分别标记。"""

    def __init__(self, primary: OpenMeteoClient, fallback: WeatherFallback | None = None) -> None:
        self.primary = primary
        self.fallback = fallback

    async def get_weather(
        self, city: City, start_date: date, end_date: date
    ) -> list[WeatherDay]:
        """返回完整日期范围，但绝不为缺失天气或温度造值。"""

        forecast_end = _today_in_shanghai() + timedelta(days=WEATHER_FORECAST_DAYS - 1)
        query_end = min(end_date, forecast_end)
        by_date: dict[date, WeatherDay] = {}
        if start_date <= query_end:
            primary = await _safe_weather(self.primary, city, start_date, query_end)
            by_date = {item.date: item for item in primary}
            if self.fallback is not None and not _covers(by_date, start_date, query_end):
                fallback = await _safe_weather(self.fallback, city, start_date, query_end)
                for item in fallback:
                    by_date.setdefault(item.date, item)
        return [
            by_date.get(
                item_date,
                WeatherDay(
                    date=item_date,
                    warning=(
                        FUTURE_FORECAST_WARNING
                        if item_date > forecast_end
                        else WEATHER_SERVICE_WARNING
                    ),
                    source=None,
                ),
            )
            for item_date in _date_range(start_date, end_date)
        ]


async def _safe_weather(
    provider: WeatherFallback,
    city: City,
    start_date: date,
    end_date: date,
) -> list[WeatherDay]:
    try:
        return await provider.get_weather(city, start_date, end_date)
    except (ProviderError, httpx.HTTPError, ValueError):
        # 天气不是行程生成的硬前置；失败将在结果中显式呈现为未知。
        return []


def _parse_weather(payload: dict[str, Any]) -> list[WeatherDay]:
    daily = payload.get("daily")
    if not isinstance(daily, dict):
        return []
    values: list[WeatherDay] = []
    for raw_date, code, high, low in zip(
        list_values(daily.get("time")),
        list_values(daily.get("weather_code")),
        list_values(daily.get("temperature_2m_max")),
        list_values(daily.get("temperature_2m_min")),
        strict=False,
    ):
        try:
            item_date = date.fromisoformat(str(raw_date))
        except ValueError:
            continue
        values.append(
            WeatherDay(
                date=item_date,
                day_weather=_wmo_text(code),
                night_weather=_wmo_text(code),
                day_temperature=_temperature(high),
                night_temperature=_temperature(low),
                source="open_meteo",
            )
        )
    return values


def _covers(values: dict[date, WeatherDay], start_date: date, end_date: date) -> bool:
    return all(item in values for item in _date_range(start_date, end_date))


def _date_range(start_date: date, end_date: date) -> list[date]:
    return [
        date.fromordinal(ordinal)
        for ordinal in range(start_date.toordinal(), end_date.toordinal() + 1)
    ]


def _wmo_text(value: Any) -> str:
    try:
        code = int(value)
    except (TypeError, ValueError):
        return "天气未知"
    labels = (
        ((0, 0), "晴"),
        ((1, 3), "多云"),
        ((45, 48), "雾"),
        ((51, 67), "小雨或冻雨"),
        ((71, 77), "降雪"),
        ((80, 82), "阵雨"),
        ((95, 99), "雷雨"),
    )
    return next((label for (low, high), label in labels if low <= code <= high), "天气未知")


def _temperature(value: Any) -> str | None:
    return str(round(float(value))) if isinstance(value, (int, float)) else None


def _today_in_shanghai() -> date:
    """返回天气查询窗口使用的中国标准日期。"""

    return datetime.now(ZoneInfo("Asia/Shanghai")).date()
