"""OpenZLTravel 的核心旅行流程。

本文件集中管理行程编排、事实校验、结果组装、预算估算和 Markdown 导出。
外部请求交给 providers，持久化交给 storage，不在这里处理 HTTP 响应。
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from app.errors import AppError, DraftError, NotFoundError
from app.models import (
    BudgetBreakdown,
    CandidateCatalog,
    DayPlan,
    DraftDay,
    HotelPlan,
    Itinerary,
    ItineraryDraft,
    MealPlan,
    RouteSegment,
    SpotPlan,
    TravelRequest,
    TripSummary,
    WeatherDay,
)
from app.providers import MapProvider, Planner
from app.storage import TripRepository

HOTEL_RATES = {"经济": 220.0, "舒适": 420.0, "品质": 760.0}


class TravelService:
    """创建、读取和删除旅行行程。"""

    def __init__(
        self,
        map_provider: MapProvider,
        planner: Planner,
        repository: TripRepository,
    ) -> None:
        self.map_provider = map_provider
        self.planner = planner
        self.repository = repository

    def create(self, request: TravelRequest) -> Itinerary:
        """生成并保存一份经过事实校验的完整行程。"""

        city = self.map_provider.resolve_city(request.destination)
        candidates = self.map_provider.search_candidates(city)
        if not candidates.attractions:
            raise AppError("no_attractions", f"暂时找不到“{city.name}”的可用景点数据", 422)
        weather = self.map_provider.get_weather(city, request.start_date, request.end_date)
        draft = self._plan_with_one_repair(request, candidates)
        days = self._assemble_days(request, draft.days, candidates, weather)
        warnings = _weather_warnings(request, weather)
        budget = estimate_budget(request, days)
        if request.budget and budget.total > request.budget:
            warnings.append(f"预估总预算 ¥{budget.total:.0f} 高于你的预算上限")
        itinerary = Itinerary(
            trip_id=uuid4(),
            destination=city.name,
            start_date=request.start_date,
            end_date=request.end_date,
            travelers=request.travelers,
            summary=draft.summary,
            days=days,
            budget=budget,
            tips=draft.tips,
            warnings=warnings,
            created_at=datetime.now(timezone.utc),
        )
        # 所有外部查询和业务校验都成功后才保存，避免历史记录出现半成品。
        self.repository.save(itinerary, request)
        return itinerary

    def _plan_with_one_repair(
        self,
        request: TravelRequest,
        candidates: CandidateCatalog,
    ) -> ItineraryDraft:
        """允许一次修复重试，在提高容错率的同时限制模型成本和不确定性。"""

        feedback: str | None = None
        for _ in range(2):
            try:
                draft = self.planner.plan(request, candidates, feedback)
                self._validate_draft(request, draft, candidates)
                return draft
            except DraftError as error:
                feedback = error.message
        raise DraftError("模型连续两次返回无效行程，请稍后重试")

    def _validate_draft(
        self,
        request: TravelRequest,
        draft: ItineraryDraft,
        candidates: CandidateCatalog,
    ) -> None:
        expected_days = set(range(1, request.days_count + 1))
        actual_days = {day.day_index for day in draft.days}
        if actual_days != expected_days or len(draft.days) != request.days_count:
            raise DraftError("模型返回的天数与旅行日期不一致")
        for day in draft.days:
            self._validate_day(day, candidates)

    @staticmethod
    def _validate_day(day: DraftDay, candidates: CandidateCatalog) -> None:
        """确认模型只引用候选池中的真实地点，并且地点类型正确。"""

        ids = [activity.poi_id for activity in day.activities]
        if len(ids) != len(set(ids)):
            raise DraftError(f"第 {day.day_index} 天存在重复景点")
        referenced_ids = [*ids, *day.meal_ids]
        if day.hotel_id:
            referenced_ids.append(day.hotel_id)
        if any(candidates.find(poi_id) is None for poi_id in referenced_ids):
            raise DraftError(f"第 {day.day_index} 天引用了候选池之外的地点")
        invalid_categories = [
            poi_id
            for poi_id in day.meal_ids
            if (poi := candidates.find(poi_id)) is not None and poi.category != "restaurant"
        ]
        if day.hotel_id and (hotel := candidates.find(day.hotel_id)) is not None:
            invalid_categories.extend([hotel.id] if hotel.category != "hotel" else [])
        if invalid_categories:
            raise DraftError(f"第 {day.day_index} 天的餐饮或住宿地点类型错误")

    def _assemble_days(
        self,
        request: TravelRequest,
        draft_days: list[DraftDay],
        candidates: CandidateCatalog,
        weather: list[WeatherDay],
    ) -> list[DayPlan]:
        weather_by_date = {item.date: item for item in weather}
        return [
            self._assemble_day(
                request,
                draft_day,
                candidates,
                weather_by_date.get(request.start_date + timedelta(days=draft_day.day_index - 1)),
            )
            for draft_day in sorted(draft_days, key=lambda item: item.day_index)
        ]

    def _assemble_day(
        self,
        request: TravelRequest,
        draft: DraftDay,
        candidates: CandidateCatalog,
        weather: WeatherDay | None,
    ) -> DayPlan:
        activities = [
            _spot_plan(
                activity.poi_id,
                activity.start_time,
                activity.duration_minutes,
                activity.note,
                candidates,
            )
            for activity in draft.activities
        ]
        routes = self._get_routes(activities, candidates)
        day_date = request.start_date + timedelta(days=draft.day_index - 1)
        return DayPlan(
            day_index=draft.day_index,
            date=day_date,
            theme=draft.theme,
            activities=activities,
            meals=_meal_plans(draft, candidates),
            hotel=_hotel_plan(draft, candidates, request.hotel_level),
            routes=routes,
            # 高德没有覆盖的日期必须明确标记未知，不能让模型补写天气事实。
            weather=weather or WeatherDay(date=day_date, warning="暂无预报"),
            notes=draft.notes,
        )

    def _get_routes(
        self,
        activities: list[SpotPlan],
        candidates: CandidateCatalog,
    ) -> list[RouteSegment]:
        """根据最终活动中的 POI ID 找回坐标，再请求路线。"""

        routes = []
        for left, right in zip(activities, activities[1:], strict=False):
            from_poi = candidates.find(left.poi_id)
            to_poi = candidates.find(right.poi_id)
            if from_poi is not None and to_poi is not None:
                routes.append(self.map_provider.get_route(from_poi, to_poi))
        return routes

    def get(self, trip_id: UUID) -> Itinerary:
        """读取完整行程，不存在时抛出稳定业务错误。"""

        itinerary = self.repository.get(trip_id)
        if itinerary is None:
            raise NotFoundError()
        return itinerary

    def list(self) -> list[TripSummary]:
        """返回历史行程摘要。"""

        return self.repository.list()

    def delete(self, trip_id: UUID) -> None:
        """删除指定行程，不存在时抛出稳定业务错误。"""

        if not self.repository.delete(trip_id):
            raise NotFoundError()


def _spot_plan(
    poi_id: str,
    start_time: str,
    duration_minutes: int,
    note: str,
    candidates: CandidateCatalog,
) -> SpotPlan:
    poi = candidates.find(poi_id)
    if poi is None:
        raise DraftError("行程引用了不存在的景点")
    return SpotPlan(
        poi_id=poi.id,
        name=poi.name,
        address=poi.address,
        latitude=poi.latitude,
        longitude=poi.longitude,
        start_time=start_time,
        duration_minutes=duration_minutes,
        note=note,
    )


def _meal_plans(draft: DraftDay, candidates: CandidateCatalog) -> list[MealPlan]:
    meal_types = ("午餐", "晚餐")
    return [
        MealPlan(
            poi_id=poi.id,
            name=poi.name,
            address=poi.address,
            latitude=poi.latitude,
            longitude=poi.longitude,
            meal_type=meal_types[index] if index < len(meal_types) else "用餐",
        )
        for index, poi_id in enumerate(draft.meal_ids)
        if (poi := candidates.find(poi_id)) is not None
    ]


def _hotel_plan(
    draft: DraftDay,
    candidates: CandidateCatalog,
    hotel_level: str,
) -> HotelPlan | None:
    if not draft.hotel_id:
        return None
    poi = candidates.find(draft.hotel_id)
    if poi is None:
        return None
    return HotelPlan(
        poi_id=poi.id,
        name=poi.name,
        address=poi.address,
        latitude=poi.latitude,
        longitude=poi.longitude,
        level=hotel_level,
    )


def _weather_warnings(request: TravelRequest, weather: list[WeatherDay]) -> list[str]:
    known_dates = {item.date for item in weather}
    missing = [
        request.start_date + timedelta(days=offset)
        for offset in range(request.days_count)
        if request.start_date + timedelta(days=offset) not in known_dates
    ]
    return [f"{item.isoformat()} 暂无天气预报" for item in missing]


# ============================================================================
# 预算估算
# ============================================================================


def estimate_budget(request: TravelRequest, days: list[DayPlan]) -> BudgetBreakdown:
    """根据行程规模生成透明的预算估算。

    高德 POI 不提供统一的门票和住宿报价，因此这里使用固定经验参数，并在
    最终结果中明确标记为估算值，避免将其误解为供应商实时报价。
    """

    nights = max(request.days_count - 1, 1)
    activity_count = sum(len(day.activities) for day in days)
    tickets = activity_count * 80 * request.travelers
    meals = request.days_count * request.travelers * 3 * 60
    hotel = nights * HOTEL_RATES[request.hotel_level]
    distance = sum(route.distance_km for day in days for route in day.routes)
    transport = max(80.0, distance * 2.5)
    other = request.travelers * 100.0
    total = tickets + meals + hotel + transport + other
    return BudgetBreakdown(
        transport=round(transport, 2),
        hotel=round(hotel, 2),
        meals=round(meals, 2),
        tickets=round(tickets, 2),
        other=round(other, 2),
        total=round(total, 2),
    )


# ============================================================================
# Markdown 导出
# ============================================================================


def itinerary_to_markdown(itinerary: Itinerary) -> str:
    """将结构化行程转换为适合保存和分享的 Markdown。"""

    lines = [
        f"# {itinerary.destination}旅行计划",
        "",
        f"{itinerary.start_date} 至 {itinerary.end_date}，共 {itinerary.travelers} 人",
        "",
        f"> {itinerary.summary}",
        "",
        "## 每日行程",
    ]
    for day in itinerary.days:
        lines.extend(["", f"### 第{day.day_index}天 · {day.date} · {day.theme}"])
        lines.append(_weather_line(day.weather))
        lines.extend(_activity_lines(day.activities))
        if day.meals:
            lines.append("- 用餐：" + "、".join(meal.name for meal in day.meals))
        if day.hotel:
            lines.append(f"- 住宿：{day.hotel.name}（{day.hotel.address}）")
        lines.extend(_route_lines(day.routes))
        lines.extend(f"- 提醒：{note}" for note in day.notes)
    lines.extend(_budget_lines(itinerary.budget))
    if itinerary.tips:
        lines.extend(["", "## 旅行建议", *[f"- {tip}" for tip in itinerary.tips]])
    if itinerary.warnings:
        lines.extend(["", "## 数据提示", *[f"- {item}" for item in itinerary.warnings]])
    return "\n".join(lines) + "\n"


def _activity_lines(activities: list[SpotPlan]) -> list[str]:
    return [
        f"- {item.start_time} **{item.name}**（{item.duration_minutes} 分钟）："
        f"{item.note or '自由游览'}，地址：{item.address}"
        for item in activities
    ]


def _route_lines(routes: list[RouteSegment]) -> list[str]:
    return [
        f"- 路线：{item.from_poi_id} → {item.to_poi_id}，"
        f"约 {item.distance_km} 公里 / {item.duration_minutes} 分钟"
        for item in routes
    ]


def _budget_lines(budget: BudgetBreakdown) -> list[str]:
    return [
        "",
        "## 预算估算",
        f"- 交通：¥{budget.transport:.0f}",
        f"- 住宿：¥{budget.hotel:.0f}",
        f"- 餐饮：¥{budget.meals:.0f}",
        f"- 门票：¥{budget.tickets:.0f}",
        f"- 其他：¥{budget.other:.0f}",
        f"- 合计：¥{budget.total:.0f}",
    ]


def _weather_line(weather: WeatherDay) -> str:
    values = weather.day_weather, weather.day_temperature
    if not any(values):
        return "天气：暂无预报"
    return f"天气：{values[0] or '未知'}，白天 {values[1] or '未知'}℃"
