"""TravelGraph 的统一领域模型。

用户需求、Provider 事实、Agent 草稿和最终行程在此拥有明确边界。
所有模型禁止未声明字段，防止 LLM 或上游响应把不受控数据带入图状态。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    """拒绝多余字段的领域模型基类。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TravelRequirements(StrictModel):
    """一次旅行的统一需求；不完整时也可存入图状态。"""

    origin: str | None = Field(default=None, max_length=40)
    destination: str | None = Field(default=None, max_length=40)
    region: str | None = Field(default=None, max_length=40)
    start_date: date | None = None
    end_date: date | None = None
    trip_days: int | None = Field(default=None, ge=1, le=7)
    travelers: int = Field(default=1, ge=1, le=20)
    budget: float | None = Field(default=None, ge=0)
    pace: Literal["轻松", "适中", "紧凑"] = "适中"
    hotel_level: Literal["经济", "舒适", "品质"] = "舒适"
    transport_mode: Literal[
        "auto", "walk", "driving", "transit", "realtime_driving"
    ] = "auto"
    preferences: list[str] = Field(default_factory=list, max_length=8)
    dietary_preferences: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def normalize_dates(self) -> "TravelRequirements":
        """在已知开始日和天数时补齐结束日，不猜测未给出的开始日。"""

        if self.start_date and self.trip_days and self.end_date is None:
            self.end_date = self.start_date + timedelta(days=self.trip_days - 1)
        if self.start_date and self.end_date:
            days = (self.end_date - self.start_date).days + 1
            if days < 1:
                raise ValueError("结束日期不能早于开始日期")
            if days > 7:
                raise ValueError("单次行程最多支持 7 天")
            self.trip_days = days
        return self

    def missing_fields(self) -> list[str]:
        """返回进入数据发现前必须补齐的字段。"""

        missing: list[str] = []
        if not self.origin:
            missing.append("origin")
        if not self.destination and not self.region:
            missing.append("destination_or_region")
        if not self.start_date:
            missing.append("start_date")
        if not self.end_date:
            missing.append("end_date")
        return missing

    @property
    def days_count(self) -> int:
        """返回已确认的行程天数。"""

        if self.start_date is None or self.end_date is None:
            return 0
        return (self.end_date - self.start_date).days + 1


class RequirementPatch(StrictModel):
    """RequirementAgent 或 interrupt 恢复输入的局部需求。"""

    origin: str | None = Field(default=None, max_length=40)
    destination: str | None = Field(default=None, max_length=40)
    region: str | None = Field(default=None, max_length=40)
    start_date: date | None = None
    end_date: date | None = None
    trip_days: int | None = Field(default=None, ge=1, le=7)
    travelers: int | None = Field(default=None, ge=1, le=20)
    budget: float | None = Field(default=None, ge=0)
    pace: Literal["轻松", "适中", "紧凑"] | None = None
    hotel_level: Literal["经济", "舒适", "品质"] | None = None
    transport_mode: Literal[
        "auto", "walk", "driving", "transit", "realtime_driving"
    ] | None = None
    preferences: list[str] | None = Field(default=None, max_length=8)
    dietary_preferences: list[str] | None = Field(default=None, max_length=5)


class City(StrictModel):
    """由 Catalog 或地图供应商确认的城市事实。"""

    name: str = Field(min_length=1, max_length=80)
    adcode: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class Poi(StrictModel):
    """可被 PlannerAgent 引用的真实地点。"""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    address: str = ""
    category: Literal["attraction", "restaurant", "hotel"]
    latitude: float
    longitude: float
    type_name: str = ""
    image_url: str | None = None
    tags: list[str] = Field(default_factory=list)


class CandidateCatalog(StrictModel):
    """某城市的真实 POI 候选池。"""

    attractions: list[Poi] = Field(default_factory=list)
    restaurants: list[Poi] = Field(default_factory=list)
    hotels: list[Poi] = Field(default_factory=list)

    @property
    def all(self) -> list[Poi]:
        """返回所有类别的 POI。"""

        return [*self.attractions, *self.restaurants, *self.hotels]

    def find(self, poi_id: str) -> Poi | None:
        """按稳定 ID 查找 POI。"""

        return next((item for item in self.all if item.id == poi_id), None)


class DestinationCandidate(StrictModel):
    """由确定性目的地评分产生的城市候选。"""

    candidate_id: str = Field(min_length=1)
    city: City
    score: float = Field(ge=0, le=1)
    reasons: list[str] = Field(default_factory=list, max_length=4)
    attraction_count: int = Field(default=0, ge=0)
    restaurant_count: int = Field(default=0, ge=0)
    hotel_count: int = Field(default=0, ge=0)


class RailSeat(StrictModel):
    """车次席别的可用性与真实报价。"""

    name: str
    availability: str = "未知"
    price: float | None = Field(default=None, ge=0)


class RailOption(StrictModel):
    """铁路 Provider 返回的可选方案。"""

    option_id: str = Field(min_length=1)
    direction: Literal["outbound", "return"]
    travel_date: date
    train_code: str
    from_station: str
    to_station: str
    departure_time: str
    arrival_time: str
    duration_minutes: int = Field(default=0, ge=0)
    seats: list[RailSeat] = Field(default_factory=list)
    price_from: float | None = Field(default=None, ge=0)
    has_ticket: bool = False
    is_transfer: bool = False
    transfer_station: str | None = None
    booking_url: str = "https://www.12306.cn/index/"


class HotelOption(StrictModel):
    """酒店 Provider 返回的真实或目录候选。"""

    hotel_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    address: str = ""
    latitude: float | None = None
    longitude: float | None = None
    star_rating: float | None = Field(default=None, ge=0, le=5)
    price_per_night: float | None = Field(default=None, ge=0)
    total_price: float | None = Field(default=None, ge=0)
    distance_km: float | None = Field(default=None, ge=0)
    image_url: str | None = None
    facilities: list[str] = Field(default_factory=list)
    source: Literal["rollinggo", "osm", "amap", "unknown"] = "unknown"
    booking_url: str | None = None


class WeatherDay(StrictModel):
    """一天的天气事实；未知值必须保持为空。"""

    date: date
    day_weather: str | None = None
    night_weather: str | None = None
    day_temperature: str | None = None
    night_temperature: str | None = None
    warning: str | None = None
    source: str | None = None


class RouteSegment(StrictModel):
    """两个真实 POI 之间的路线事实或明确标记的估算。"""

    from_poi_id: str
    to_poi_id: str
    distance_km: float = Field(ge=0)
    duration_minutes: int = Field(ge=0)
    mode: str
    cost: float | None = Field(default=None, ge=0)
    polyline: list[tuple[float, float]] = Field(default_factory=list)
    source: Literal["amap", "local_estimate", "unknown"] = "unknown"


class RailChoice(StrictModel):
    """用户对一趟车次和席别的选择。"""

    option_id: str
    seat_type: str | None = None


class TravelSelection(StrictModel):
    """交通与住宿 interrupt 恢复后的受控选择。"""

    outbound: RailChoice | None = None
    return_trip: RailChoice | None = None
    hotel_id: str | None = None
    self_arranged_outbound: bool = False
    self_arranged_return: bool = False
    self_arranged_hotel: bool = False


class ActivityDraft(StrictModel):
    """PlannerAgent 产生的单个活动，只允许引用 POI ID。"""

    poi_id: str
    start_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    duration_minutes: int = Field(default=120, ge=30, le=480)
    note: str = Field(default="", max_length=160)


class DayDraft(StrictModel):
    """一天的受控规划草稿。"""

    day_index: int = Field(ge=1, le=7)
    theme: str = Field(min_length=1, max_length=80)
    activities: list[ActivityDraft] = Field(default_factory=list, max_length=4)
    meal_ids: list[str] = Field(default_factory=list, max_length=2)
    hotel_id: str | None = None
    notes: list[str] = Field(default_factory=list, max_length=4)


class ItineraryDraft(StrictModel):
    """PlannerAgent 的结构化输出。"""

    summary: str = Field(min_length=1, max_length=300)
    days: list[DayDraft] = Field(min_length=1, max_length=7)
    tips: list[str] = Field(default_factory=list, max_length=8)


class ReviewIssue(StrictModel):
    """ReviewAgent 发现的一条可定位问题。"""

    code: str
    message: str
    day_index: int | None = Field(default=None, ge=1, le=7)
    severity: Literal["warning", "error"] = "warning"


class ReviewResult(StrictModel):
    """ReviewAgent 的受控审查结果。"""

    passed: bool
    issues: list[ReviewIssue] = Field(default_factory=list, max_length=12)
    revision_instruction: str | None = Field(default=None, max_length=600)


class TravelFacts(StrictModel):
    """图中所有 Provider 事实的聚合，不保存原始响应。"""

    city: City | None = None
    catalog: CandidateCatalog | None = None
    outbound_options: list[RailOption] = Field(default_factory=list)
    return_options: list[RailOption] = Field(default_factory=list)
    hotel_options: list[HotelOption] = Field(default_factory=list)
    weather: list[WeatherDay] = Field(default_factory=list)
    routes: dict[int, list[RouteSegment]] = Field(default_factory=dict)


class BudgetBreakdown(StrictModel):
    """事实报价与明示经验估算的预算汇总。"""

    intercity_transport: float | None = None
    local_transport: float | None = None
    hotel: float | None = None
    meals_estimated: float = 0
    tickets_estimated: float = 0
    total_known: float = 0
    currency: Literal["CNY"] = "CNY"


class PlaceSnapshot(StrictModel):
    """从 Provider 事实复制的最小展示快照，不接受 Agent 文案。"""

    fact_id: str
    name: str
    address: str = ""
    category: Literal["attraction", "restaurant", "hotel"]
    latitude: float | None = None
    longitude: float | None = None
    image_url: str | None = None


class TripRecord(StrictModel):
    """最终校验后写入 Store 的完整行程。"""

    trip_id: UUID
    user_id: str
    requirements: TravelRequirements
    city: City
    selection: TravelSelection
    draft: ItineraryDraft
    weather: list[WeatherDay]
    routes: dict[int, list[RouteSegment]]
    budget: BudgetBreakdown
    place_index: dict[str, PlaceSnapshot] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
