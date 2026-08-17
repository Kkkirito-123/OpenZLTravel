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
    transport_mode: Literal["auto", "walk", "driving", "transit", "realtime_driving"] = "auto"
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


class PlanningRequest(TravelRequest):
    """三阶段工作台使用的旅行需求，额外要求明确出发地。"""

    origin: str = Field(min_length=1, max_length=40, description="出发城市或车站")


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


class DataSource(BaseModel):
    """说明一条事实来自哪里以及它的新鲜程度。"""

    provider: Literal["open_meteo", "amap", "local_estimate", "osm", "unknown"]
    freshness: Literal["static", "forecast", "realtime", "estimated"]
    fetched_at: datetime | None = None


class WeatherDay(BaseModel):
    """某一天的天气；没有预报时通过 warning 明确标注。"""

    date: date
    day_weather: str | None = None
    night_weather: str | None = None
    day_temperature: str | None = None
    night_temperature: str | None = None
    warning: str | None = None
    source: DataSource | None = None


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
    activities: list[DraftActivity] = Field(default_factory=list, max_length=4)
    meal_ids: list[str] = Field(default_factory=list, max_length=2)
    hotel_id: str | None = None
    notes: list[str] = Field(default_factory=list, max_length=4)


class ItineraryDraft(BaseModel):
    """模型返回的受控草稿。"""

    summary: str = Field(min_length=1, max_length=300)
    days: list[DraftDay] = Field(min_length=1, max_length=7)
    tips: list[str] = Field(default_factory=list, max_length=8)


class CopyEnhancement(BaseModel):
    """模型可选润色结果，只包含不改变旅行事实的文案字段。"""

    summary: str = Field(min_length=1, max_length=300)
    themes: list[str] = Field(min_length=1, max_length=7)
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
    source: DataSource | None = None
    transit_lines: list["TransitLine"] = Field(default_factory=list)
    via_poi_ids: list[str] = Field(default_factory=list)


class TransitLine(BaseModel):
    """一条公交或地铁线路及其上下车站信息。"""

    name: str
    type: str
    departure_stop: str
    arrival_stop: str
    via_stops: list[str] = Field(default_factory=list)


class BudgetBreakdown(BaseModel):
    """估算预算明细。"""

    transport: float = 0
    local_transport: float | None = None
    intercity_transport: float | None = None
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
    planning_session_id: UUID | None = None
    revision: int = Field(default=1, ge=1)
    destination: str
    start_date: date
    end_date: date
    travelers: int
    summary: str
    days: list[DayPlan]
    budget: BudgetBreakdown
    intercity: "IntercityPlan | None" = None
    accommodation: "AccommodationPlan | None" = None
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


# 规划会话、车票与酒店


class RailSeat(BaseModel):
    """一个席别的余票和价格；未知价格保持为空，不参与预算。"""

    name: str
    availability: str = "未知"
    price: float | None = None


class RailSegment(BaseModel):
    """直达车次或中转方案中的一段铁路行程。"""

    train_code: str
    from_station: str
    to_station: str
    departure_time: str
    arrival_time: str
    duration_minutes: int = Field(default=0, ge=0)
    seats: list[RailSeat] = Field(default_factory=list)


class RailOption(BaseModel):
    """可供用户选择的往返车次或一次中转方案。"""

    option_id: str
    direction: Literal["outbound", "return"]
    travel_date: date
    train_code: str
    train_type: str = "列车"
    from_station: str
    to_station: str
    departure_time: str
    arrival_time: str
    duration_minutes: int = Field(default=0, ge=0)
    seats: list[RailSeat] = Field(default_factory=list)
    price_from: float | None = None
    has_ticket: bool = False
    is_transfer: bool = False
    transfer_station: str | None = None
    segments: list[RailSegment] = Field(default_factory=list)
    booking_url: str = "https://www.12306.cn/index/"


class RailChoice(BaseModel):
    """用户选择的一趟车和可选席别。"""

    option_id: str
    seat_type: str | None = None


class HotelOption(BaseModel):
    """酒店搜索结果；实时服务不可用时可以来自本地 OSM 目录。"""

    hotel_id: str
    name: str
    address: str = ""
    latitude: float | None = None
    longitude: float | None = None
    star_rating: float | None = None
    price_per_night: float | None = None
    total_price: float | None = None
    distance_km: float | None = None
    image_url: str | None = None
    facilities: list[str] = Field(default_factory=list)
    source: Literal["rollinggo", "dida", "osm"]
    booking_url: str | None = None


class HotelRoom(BaseModel):
    """酒店详情中的可售房型与退改摘要。"""

    room_id: str
    name: str
    price: float | None = None
    breakfast: str | None = None
    cancellation: str | None = None
    available: bool = True


class HotelDetail(BaseModel):
    """按需加载的酒店详情，不在首次发现阶段批量查询。"""

    hotel_id: str
    name: str
    address: str = ""
    description: str = ""
    facilities: list[str] = Field(default_factory=list)
    images: list[str] = Field(default_factory=list)
    rooms: list[HotelRoom] = Field(default_factory=list)
    booking_url: str | None = None
    source: Literal["rollinggo", "dida", "osm"]


class PlanningSelection(BaseModel):
    """用户在生成前确认的往返交通和住宿。"""

    outbound: RailChoice | None = None
    return_trip: RailChoice | None = None
    hotel_id: str | None = None
    self_arranged_outbound: bool = False
    self_arranged_return: bool = False
    self_arranged_hotel: bool = False


class IntercityPlan(BaseModel):
    """最终行程中已经确认的往返城际交通。"""

    outbound: RailOption | None = None
    return_trip: RailOption | None = None
    self_arranged_outbound: bool = False
    self_arranged_return: bool = False


class AccommodationPlan(BaseModel):
    """最终行程中的住宿选择。"""

    hotel: HotelOption | None = None
    check_in: date
    check_out: date
    nights: int = Field(default=0, ge=0)
    self_arranged: bool = False


StepStatus = Literal["pending", "running", "completed", "degraded", "failed", "cancelled"]
SessionStatus = Literal[
    "searching",
    "awaiting_selection",
    "generating",
    "completed",
    "failed",
    "cancelled",
]


class PlanningStep(BaseModel):
    """规划会话中可独立展示和重试的一个步骤。"""

    name: str
    label: str
    status: StepStatus = "pending"
    attempts: int = 0
    duration_ms: int | None = None
    cache_hit: bool = False
    message: str | None = None
    error_code: str | None = None


class PlanningSession(BaseModel):
    """可轮询、可恢复的旅行规划会话快照。"""

    session_id: UUID
    status: SessionStatus = "searching"
    request: PlanningRequest
    city: City | None = None
    steps: list[PlanningStep] = Field(default_factory=list)
    outbound_options: list[RailOption] = Field(default_factory=list)
    return_options: list[RailOption] = Field(default_factory=list)
    outbound_transfers: list[RailOption] = Field(default_factory=list)
    return_transfers: list[RailOption] = Field(default_factory=list)
    hotel_options: list[HotelOption] = Field(default_factory=list)
    weather: list[WeatherDay] = Field(default_factory=list)
    candidates: CandidateCatalog | None = None
    selection: PlanningSelection = Field(default_factory=PlanningSelection)
    trip_id: UUID | None = None
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class DayActivityEdit(BaseModel):
    """用户编辑后的一条有序景点安排。"""

    poi_id: str
    start_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    duration_minutes: int = Field(ge=30, le=480)


class DayEditRequest(BaseModel):
    """带乐观锁版本号的单日编辑请求。"""

    expected_revision: int = Field(ge=1)
    activities: list[DayActivityEdit] = Field(max_length=4)


class TransferSearchRequest(BaseModel):
    """按方向主动展开一次中转方案。"""

    direction: Literal["outbound", "return"]


class TripAlternatives(BaseModel):
    """行程编辑时可替换的真实景点候选。"""

    trip_id: UUID
    revision: int
    attractions: list[Poi]


# 多轮旅行助手

AssistantFlow = Literal["destination_discovery", "trip_planning"]
AssistantSkillId = Literal["destination_discovery", "trip_planning"]
AssistantStatus = Literal["collecting", "recommendation_ready", "planning_started", "closed"]
MemorySlotName = Literal[
    "origin",
    "preferences",
    "dietary_preferences",
    "pace",
    "hotel_level",
    "transport_mode",
]
SlotSource = Literal["user_explicit", "deterministic", "memory", "default"]


class AssistantSkillView(BaseModel):
    """前端可展示的 Skill 契约，不包含提示词或供应商调用参数。"""

    id: AssistantSkillId
    title: str
    description: str
    required_slots: list[str] = Field(default_factory=list)
    effect: Literal["collect_requirements", "start_planning"]


class AssistantTokenUsage(BaseModel):
    """会话内已成功消费的意图模型 Token 账本。"""

    model_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class TravelMemory(BaseModel):
    """用户明确要求长期保存的一项稳定旅行偏好。"""

    key: MemorySlotName
    value: str | list[str]
    version: int = Field(ge=1)
    source_session_id: UUID
    created_at: datetime
    updated_at: datetime


class TravelDialogueSlots(BaseModel):
    """旅行对话中已经确认的用户需求，不保存供应商事实。"""

    origin: str | None = Field(default=None, max_length=40)
    destination_region: str | None = Field(default=None, max_length=40)
    destination_city: str | None = Field(default=None, max_length=40)
    start_date: date | None = None
    end_date: date | None = None
    days: int | None = Field(default=None, ge=1, le=7)
    budget: float | None = Field(default=None, ge=0)
    travelers: int = Field(default=1, ge=1, le=20)
    preferences: list[str] = Field(default_factory=list, max_length=8)
    dietary_preferences: list[str] = Field(default_factory=list, max_length=5)
    distance_preference: Literal["near", "far"] | None = None
    pace: Literal["轻松", "适中", "紧凑"] = "适中"
    hotel_level: Literal["经济", "舒适", "品质"] = "舒适"
    transport_mode: Literal["auto", "walk", "driving", "transit", "realtime_driving"] = "auto"
    notes: str = Field(default="", max_length=500)


class SlotMetadata(BaseModel):
    """记录槽位来自用户、确定性推导还是默认值。"""

    source: SlotSource
    updated_turn: int = Field(ge=0)


class TravelDialogueState(BaseModel):
    """多轮旅行助手的权威状态；聊天摘要不能覆盖这里的值。"""

    session_id: UUID
    revision: int = Field(default=0, ge=0)
    status: AssistantStatus = "collecting"
    active_flow: AssistantFlow | None = None
    slots: TravelDialogueSlots = Field(default_factory=TravelDialogueSlots)
    slot_metadata: dict[str, SlotMetadata] = Field(default_factory=dict)
    pending_slots: list[str] = Field(default_factory=list)
    last_question: str | None = None
    planning_session_id: UUID | None = None
    token_usage: AssistantTokenUsage = Field(default_factory=AssistantTokenUsage)
    created_at: datetime
    updated_at: datetime


class AssistantConversationTurn(BaseModel):
    """供前端恢复聊天页面的一轮用户与助手消息。"""

    sequence: int = Field(ge=1)
    user_content: str
    assistant_content: str
    created_at: datetime


class AssistantMessageRequest(BaseModel):
    """一条带幂等标识的用户消息。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    message_id: UUID
    content: str = Field(min_length=1, max_length=1000)


class VisitorClaimRequest(BaseModel):
    """提交一次性旧数据认领码。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    token: str = Field(min_length=16, max_length=256)


class AssistantTurnResponse(BaseModel):
    """处理一条消息后的回复和最新权威状态。"""

    message_id: UUID
    reply: str
    state: TravelDialogueState
    missing_slots: list[str] = Field(default_factory=list)
    planning_session_id: UUID | None = None
    skill: AssistantSkillView | None = None
    command_source: Literal["fast_parser", "intent_cache", "llm"] = "fast_parser"
    context_tokens: int = Field(default=0, ge=0)


class AssistantSessionView(BaseModel):
    """助手会话状态和可恢复的近期对话。"""

    state: TravelDialogueState
    turns: list[AssistantConversationTurn] = Field(default_factory=list)
    skill: AssistantSkillView | None = None
    memories: list[TravelMemory] = Field(default_factory=list)
