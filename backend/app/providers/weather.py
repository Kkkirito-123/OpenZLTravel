"""Open-Meteo 天气 Provider。

天气主来源不需要高德 Key。这里只解析可验证的 WMO 预报数据；日期缺失或格式异常时返回
已有有效记录，由上层决定是否使用高德兜底或显示“暂无预报”。
"""

from __future__ import annotations

from datetime import date, datetime
from threading import Lock
from typing import Any, cast

import httpx

from app.config import Settings
from app.errors import ProviderError
from app.models import City, DataSource, WeatherDay
from app.providers.base import CacheStore, stable_key
from app.providers.geo import list_values, now


class OpenMeteoClient:
    """无需 API Key 的天气预报客户端，负责把 WMO 代码转成中文事实。"""

    CACHE_TTL_SECONDS = 30 * 60

    def __init__(self, settings: Settings, cache_store: CacheStore | None = None) -> None:
        self.settings = settings
        self.cache_store = cache_store
        self.request_lock = Lock()
        self.http = httpx.Client(timeout=settings.open_meteo_timeout_seconds)

    def close(self) -> None:
        """关闭天气客户端的连接池；不依赖垃圾回收时机。"""

        self.http.close()

    def get_weather(self, city: City, start_date: date, end_date: date) -> list[WeatherDay]:
        """查询每日最高最低温和天气代码，日期不覆盖时返回已有结果。"""

        if city.latitude is None or city.longitude is None:
            raise ProviderError("weather_unavailable", "目的地缺少天气查询坐标")
        key = stable_key(
            round(city.latitude, 5),
            round(city.longitude, 5),
            start_date,
            end_date,
        )
        payload = self._cached_payload(key)
        if payload is None:
            payload = self._fetch_once(key, city, start_date, end_date)
        return _open_meteo_weather(payload)

    def _fetch_once(
        self,
        key: str,
        city: City,
        start_date: date,
        end_date: date,
    ) -> dict[str, Any]:
        """合并重复请求；缓存损坏或缺失时再发起网络调用。"""

        with self.request_lock:
            cached = self._cached_payload(key)
            if cached is not None:
                return cached
            payload = {
                **self._request(city, start_date, end_date),
                "_openzl_fetched_at": now().isoformat(),
            }
            if self.cache_store is not None:
                self.cache_store.set_cache("open_meteo", key, payload, self.CACHE_TTL_SECONDS)
            return payload

    def _cached_payload(self, key: str) -> dict[str, Any] | None:
        if self.cache_store is None:
            return None
        value = self.cache_store.get_cache("open_meteo", key)
        return cast(dict[str, Any], value) if isinstance(value, dict) else None

    def _request(self, city: City, start_date: date, end_date: date) -> dict[str, Any]:
        try:
            response = self.http.get(
                self.settings.open_meteo_base_url,
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
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError("weather_unavailable", "Open-Meteo 天气服务暂时不可用") from exc
        if not isinstance(payload, dict):
            raise ProviderError("weather_unavailable", "Open-Meteo 返回了无法识别的数据")
        return cast(dict[str, Any], payload)


def covers_dates(weather: list[WeatherDay], start_date: date, end_date: date) -> bool:
    """判断天气主来源是否覆盖请求中的每一天。"""

    dates = {item.date for item in weather}
    return all(
        start_date <= item <= end_date and item in dates
        for item in _date_range(start_date, end_date)
    )


def _open_meteo_weather(payload: dict[str, Any]) -> list[WeatherDay]:
    """把缓存或网络响应统一转换为领域天气事实。"""

    daily = payload.get("daily", {})
    if not isinstance(daily, dict):
        return []
    fetched_at = _source_time(payload.get("_openzl_fetched_at"))
    weather: list[WeatherDay] = []
    for item_date, code, high, low in zip(
        list_values(daily.get("time")),
        list_values(daily.get("weather_code")),
        list_values(daily.get("temperature_2m_max")),
        list_values(daily.get("temperature_2m_min")),
        strict=False,
    ):
        try:
            forecast_date = date.fromisoformat(str(item_date))
        except (TypeError, ValueError):
            continue
        weather.append(
            WeatherDay(
                date=forecast_date,
                day_weather=_wmo_text(code),
                night_weather=_wmo_text(code),
                day_temperature=_temperature(high),
                night_temperature=_temperature(low),
                source=DataSource(
                    provider="open_meteo",
                    freshness="forecast",
                    fetched_at=fetched_at,
                ),
            )
        )
    return weather


def _source_time(value: Any) -> datetime:
    """恢复缓存中的首次抓取时间，旧缓存缺失时使用当前时间。"""

    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return now()


def _date_range(start_date: date, end_date: date) -> list[date]:
    return [
        start_date.fromordinal(value)
        for value in range(start_date.toordinal(), end_date.toordinal() + 1)
    ]


def _wmo_text(code: Any) -> str:
    try:
        number = int(code)
    except (TypeError, ValueError):
        return "天气未知"
    ranges = (
        ((0, 0), "晴"),
        ((1, 3), "多云"),
        ((45, 48), "雾"),
        ((51, 67), "小雨或冻雨"),
        ((71, 77), "降雪"),
        ((80, 82), "阵雨"),
        ((95, 99), "雷雨"),
    )
    return next((label for (lower, upper), label in ranges if lower <= number <= upper), "天气未知")


def _temperature(value: Any) -> str | None:
    return str(round(float(value))) if isinstance(value, (int, float)) else None
