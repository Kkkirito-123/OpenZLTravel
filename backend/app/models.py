"""OpenZLTravel 的统一数据模型。

本文件按用户输入、外部事实、模型草稿和最终行程分区，只描述业务数据。
它不依赖 FastAPI、数据库或具体供应商，以保持数据边界稳定、便于测试。
"""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# 用户输入


class TravelRequest(BaseModel):
    """用户提交的旅行需求。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    destination: str = Field(min_length=1, max_length=40, description="目的地城市")
    start_date: date
    end_date: date
    travelers: int = Field(default=1, ge=1, le=20, description="出行人数")
    budget: float = Field(default=0, ge=0, description="总预算，0 表示不设上限")
    pace: Literal["轻松", "适中", "紧凑"] = "适中"
    hotel_level: Literal["经济", "舒适", "品质"] = "舒适"
    preferences: list[str] = Field(default_factory=list, max_length=8)
    dietary_preferences: list[str] = Field(default_factory=list, max_length=5)
    notes: str = Field(default="", max_length=500)

    @field_validator("preferences", "dietary_preferences")
    @classmethod
    def remove_empty_items(cls, values: list[str]) -> list[str]:
        """去掉空标签，避免无意义数据进入模型提示词。"""

        return [item for item in values if item]

    @model_validator(mode="after")
    def validate_date_range(self) -> "TravelRequest":
        """限制行程日期必须连续且不超过七天。"""

        days = (self.end_date - self.start_date).days + 1
        if days < 1:
            raise ValueError("结束日期不能早于开始日期")
        if days > 7:
            raise ValueError("单次行程最多支持 7 天")
        return self

    @property
    def days_count(self) -> int:
        """返回包含首尾日期的旅行天数。"""

        return (self.end_date - self.start_date).days + 1


# 外部事实


class City(BaseModel):
    """地图服务确认后的城市信息。"""

    name: str
    adcode: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class Poi(BaseModel):
    """来自地图服务的真实地点候选。"""

    id: str = Field(min_length=1)
    name: str
    address: str = ""
    category: Literal["attraction", "restaurant", "hotel"]
    latitude: float
    longitude: float
    type_name: str = ""
    image_url: str | None = None


class CandidateCatalog(BaseModel):
    """供模型选择的地点候选池。"""

    attractions: list[Poi] = Field(default_factory=list)
    restaurants: list[Poi] = Field(default_factory=list)
    hotels: list[Poi] = Field(default_factory=list)

    def find(self, poi_id: str) -> Poi | None:
        """按地图 POI ID 查找地点，模型只能引用这里的记录。"""

        return next((poi for poi in self.all if poi.id == poi_id), None)

    @property
    def all(self) -> list[Poi]:
        """返回不区分类别的完整候选列表。"""

        return [*self.attractions, *self.restaurants, *self.hotels]

    def prompt_data(self) -> dict[str, list[dict[str, object]]]:
        """生成只包含必要事实的模型输入，图片不参与规划决策。"""

        return {
            "attractions": [poi.model_dump(exclude={"image_url"}) for poi in self.attractions],
            "restaurants": [poi.model_dump(exclude={"image_url"}) for poi in self.restaurants],
            "hotels": [poi.model_dump(exclude={"image_url"}) for poi in self.hotels],
        }


class Coordinate(BaseModel):
    """地图轨迹点。"""

    latitude: float
    longitude: float


class WeatherDay(BaseModel):
    """某一天的天气；没有预报时通过 warning 明确标注。"""

    date: date
    day_weather: str | None = None
    night_weather: str | None = None
    day_temperature: str | None = None
    night_temperature: str | None = None
    warning: str | None = None


# 模型规划草稿


class DraftActivity(BaseModel):
    """模型规划的景点安排，只允许引用候选 POI ID。"""

    poi_id: str
    start_time: str = "09:00"
    duration_minutes: int = Field(default=120, ge=30, le=480)
    note: str = Field(default="", max_length=160)


class DraftDay(BaseModel):
    """模型规划的一天。"""

    day_index: int = Field(ge=1, le=7)
    theme: str = Field(min_length=1, max_length=80)
    activities: list[DraftActivity] = Field(min_length=1, max_length=4)
    meal_ids: list[str] = Field(default_factory=list, max_length=2)
    hotel_id: str | None = None
    notes: list[str] = Field(default_factory=list, max_length=4)


class ItineraryDraft(BaseModel):
    """模型返回的受控草稿。"""

    summary: str = Field(min_length=1, max_length=300)
    days: list[DraftDay] = Field(min_length=1, max_length=7)
    tips: list[str] = Field(default_factory=list, max_length=8)


# 最终行程


class SpotPlan(BaseModel):
    """补全真实 POI 后的景点安排。"""

    poi_id: str
    name: str
    address: str
    latitude: float
    longitude: float
    start_time: str
    duration_minutes: int
    note: str = ""
    image_url: str | None = None


class MealPlan(BaseModel):
    """来自候选餐饮 POI 的用餐建议。"""

    poi_id: str
    name: str
    address: str
    latitude: float
    longitude: float
    meal_type: str
    image_url: str | None = None


class HotelPlan(BaseModel):
    """来自候选酒店 POI 的住宿建议。"""

    poi_id: str
    name: str
    address: str
    latitude: float
    longitude: float
    level: str
    image_url: str | None = None


class RouteSegment(BaseModel):
    """两个相邻景点之间的地图路线。"""

    from_poi_id: str
    to_poi_id: str
    distance_km: float
    duration_minutes: int
    mode: str = "驾车"
    polyline: list[Coordinate] = Field(default_factory=list)


class BudgetBreakdown(BaseModel):
    """估算预算明细。"""

    transport: float = 0
    hotel: float = 0
    meals: float = 0
    tickets: float = 0
    other: float = 0
    total: float = 0


class DayPlan(BaseModel):
    """最终的一日行程；旧记录没有每日预算时保持为空。"""

    day_index: int
    date: date
    theme: str
    activities: list[SpotPlan]
    meals: list[MealPlan] = Field(default_factory=list)
    hotel: HotelPlan | None = None
    routes: list[RouteSegment] = Field(default_factory=list)
    weather: WeatherDay
    budget: BudgetBreakdown | None = None
    notes: list[str] = Field(default_factory=list)


class Itinerary(BaseModel):
    """对外展示和保存的完整行程。"""

    trip_id: UUID
    destination: str
    start_date: date
    end_date: date
    travelers: int
    summary: str
    days: list[DayPlan]
    budget: BudgetBreakdown
    tips: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime


# 历史摘要


class TripSummary(BaseModel):
    """历史记录列表中的轻量行程摘要。"""

    trip_id: UUID
    destination: str
    start_date: date
    end_date: date
    summary: str
    created_at: datetime
