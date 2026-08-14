"""OpenZLTravel 的核心旅行流程。

本文件集中管理行程编排、事实校验、结果组装、预算估算和 Markdown 导出。
外部请求交给 providers，持久化交给 storage，不在这里处理 HTTP 响应。
"""

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import cast
from uuid import UUID, uuid4

from app.errors import AppError, ConflictError, DraftError, NotFoundError
from app.models import (
    AccommodationPlan,
    BudgetBreakdown,
    CandidateCatalog,
    City,
    DayActivityEdit,
    DayEditRequest,
    DayPlan,
    DraftDay,
    HotelPlan,
    IntercityPlan,
    Itinerary,
    ItineraryDraft,
    MealPlan,
    Poi,
    RouteSegment,
    SpotPlan,
    TravelRequest,
    TripAlternatives,
    TripSummary,
    WeatherDay,
)
from app.providers import MapProvider, Planner, TransportResult
from app.storage import TripRepository

HOTEL_RATES = {"经济": 220.0, "舒适": 420.0, "品质": 760.0}
TICKET_RATE = 80.0
MEAL_RATE = 60.0
DAILY_TRANSPORT_MINIMUM = 40.0
DISTANCE_RATE = 2.5
OTHER_RATE = 100.0


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
        itinerary = self._build_itinerary(request, city, draft, candidates, weather, None)
        # 所有外部查询和业务校验都成功后才保存，避免历史记录出现半成品。
        self.repository.save(itinerary, request)
        return itinerary

    def build_itinerary(
        self,
        request: TravelRequest,
        city: City,
        candidates: CandidateCatalog,
        draft: ItineraryDraft,
        weather: list[WeatherDay],
        transport: Mapping[int, TransportResult],
        transport_warnings: list[str] | None = None,
    ) -> Itinerary:
        """根据工作流已经取得的事实组装完整行程，但不触发保存。"""

        return self._build_itinerary(
            request,
            city,
            draft,
            candidates,
            weather,
            transport,
            transport_warnings,
        )

    def _build_itinerary(
        self,
        request: TravelRequest,
        city: City,
        draft: ItineraryDraft,
        candidates: CandidateCatalog,
        weather: list[WeatherDay],
        transport: Mapping[int, TransportResult] | None,
        transport_warnings: list[str] | None = None,
        *,
        trip_id: UUID | None = None,
        planning_session_id: UUID | None = None,
        intercity: IntercityPlan | None = None,
        accommodation: AccommodationPlan | None = None,
    ) -> Itinerary:
        raw_days = self._assemble_days(request, draft.days, candidates, weather, transport)
        raw_days = _apply_accommodation(raw_days, accommodation, request.hotel_level)
        days, budget = estimate_budgets(request, raw_days)
        warnings = _weather_warnings(request, weather)
        warnings.extend(_budget_warnings(request, days, accommodation))
        warnings.extend(transport_warnings or [])
        if request.budget and budget.total > request.budget:
            warnings.append(f"预估总预算 ¥{budget.total:.0f} 高于你的预算上限")
        days, budget = _apply_selected_costs(
            days, budget, request, intercity, accommodation
        )
        itinerary = Itinerary(
            trip_id=trip_id or uuid4(),
            planning_session_id=planning_session_id,
            destination=city.name,
            start_date=request.start_date,
            end_date=request.end_date,
            travelers=request.travelers,
            summary=draft.summary,
            days=days,
            budget=budget,
            intercity=intercity,
            accommodation=accommodation,
            tips=draft.tips,
            warnings=warnings,
            created_at=datetime.now(timezone.utc),
        )
        return itinerary

    def build_workbench_itinerary(
        self,
        request: TravelRequest,
        city: City,
        candidates: CandidateCatalog,
        draft: ItineraryDraft,
        weather: list[WeatherDay],
        transport: Mapping[int, TransportResult],
        session_id: UUID,
        intercity: IntercityPlan,
        accommodation: AccommodationPlan,
        warnings: list[str],
    ) -> Itinerary:
        """组装工作台结果，事实选择和会话 ID 均由运行时提供。"""

        return self._build_itinerary(
            request,
            city,
            draft,
            candidates,
            weather,
            transport,
            warnings,
            trip_id=session_id,
            planning_session_id=session_id,
            intercity=intercity,
            accommodation=accommodation,
        )

    async def create_async(self, request: TravelRequest) -> Itinerary:
        """通过 LangGraph 编排异步生成；测试替身仍兼容旧同步接口。"""

        if not hasattr(self.map_provider, "get_transport_async"):
            import asyncio

            return await asyncio.to_thread(self.create, request)
        from app.workflow import TravelWorkflow

        itinerary = await TravelWorkflow(self).run(request)
        # 只有图中所有事实和规则校验完成后才写入一次，避免半成品历史记录。
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
        transport: Mapping[int, TransportResult] | None = None,
    ) -> list[DayPlan]:
        weather_by_date = {item.date: item for item in weather}
        return [
            self._assemble_day(
                request,
                draft_day,
                candidates,
                weather_by_date.get(request.start_date + timedelta(days=draft_day.day_index - 1)),
                transport.get(draft_day.day_index) if transport else None,
            )
            for draft_day in sorted(draft_days, key=lambda item: item.day_index)
        ]

    def _assemble_day(
        self,
        request: TravelRequest,
        draft: DraftDay,
        candidates: CandidateCatalog,
        weather: WeatherDay | None,
        transport: TransportResult | None = None,
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
        routes = (
            transport.routes
            if transport is not None
            else self._get_routes(activities, candidates)
        )
        day_date = request.start_date + timedelta(days=draft.day_index - 1)
        return DayPlan(
            day_index=draft.day_index,
            date=day_date,
            theme=draft.theme,
            activities=activities,
            meals=_meal_plans(draft, candidates),
            hotel=(
                _hotel_plan(draft, candidates, request.hotel_level)
                if day_date < request.end_date
                else None
            ),
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

    def alternatives(self, trip_id: UUID, candidates: CandidateCatalog) -> TripAlternatives:
        """返回编辑时允许引用的真实候选景点。"""

        itinerary = self.get(trip_id)
        return TripAlternatives(
            trip_id=trip_id,
            revision=itinerary.revision,
            attractions=candidates.attractions,
        )

    async def edit_day(
        self,
        trip_id: UUID,
        day_index: int,
        edit: DayEditRequest,
        candidates: CandidateCatalog,
    ) -> Itinerary:
        """编辑一天并只重算该日路线与全程预算。"""

        itinerary = self.get(trip_id)
        request = self.repository.get_request(trip_id)
        if request is None:
            raise NotFoundError()
        if itinerary.revision != edit.expected_revision:
            raise ConflictError()
        if not 1 <= day_index <= len(itinerary.days):
            raise AppError("day_not_found", "行程中不存在这一天", 404)
        activities = [_edited_spot(item, candidates) for item in edit.activities]
        pois = [candidates.find(item.poi_id) for item in activities]
        routes = await _edited_routes(
            self.map_provider,
            City(name=itinerary.destination),
            [poi for poi in pois if poi],
            request.transport_mode,
        )
        changed = itinerary.days[day_index - 1].model_copy(
            update={"activities": activities, "routes": routes}
        )
        days = list(itinerary.days)
        days[day_index - 1] = changed
        budgeted_days, budget = estimate_budgets(request, days)
        budgeted_days, budget = _apply_selected_costs(
            budgeted_days,
            budget,
            request,
            itinerary.intercity,
            itinerary.accommodation,
        )
        updated = itinerary.model_copy(
            update={"days": budgeted_days, "budget": budget, "revision": itinerary.revision + 1}
        )
        self.repository.save(updated, request)
        return updated


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
        image_url=poi.image_url,
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
            image_url=poi.image_url,
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
        image_url=poi.image_url,
    )


def _weather_warnings(request: TravelRequest, weather: list[WeatherDay]) -> list[str]:
    known_dates = {item.date for item in weather}
    missing = [
        request.start_date + timedelta(days=offset)
        for offset in range(request.days_count)
        if request.start_date + timedelta(days=offset) not in known_dates
    ]
    return [f"{item.isoformat()} 暂无天气预报" for item in missing]


def _budget_warnings(
    request: TravelRequest,
    days: list[DayPlan],
    accommodation: AccommodationPlan | None = None,
) -> list[str]:
    """提醒用户哪些住宿夜没有进入预算。"""

    if accommodation and accommodation.self_arranged:
        return ["住宿由用户自行安排，预算未包含住宿费用。"]
    if accommodation and accommodation.hotel:
        return []
    return [
        f"{day.date.isoformat()} 未安排住宿，预算未包含该晚住宿费用"
        for day in days
        if day.date < request.end_date and day.hotel is None
    ]


def _apply_accommodation(
    days: list[DayPlan],
    accommodation: AccommodationPlan | None,
    hotel_level: str,
) -> list[DayPlan]:
    """把已选酒店投影到住宿夜；缺少坐标时只保留顶部住宿事实。"""

    if not accommodation or not accommodation.hotel or accommodation.self_arranged:
        return days
    hotel = accommodation.hotel
    if hotel.latitude is None or hotel.longitude is None:
        return days
    plan = HotelPlan(
        poi_id=hotel.hotel_id,
        name=hotel.name,
        address=hotel.address,
        latitude=hotel.latitude,
        longitude=hotel.longitude,
        level=hotel_level,
        image_url=hotel.image_url,
    )
    return [
        day.model_copy(update={"hotel": plan}) if day.date < accommodation.check_out else day
        for day in days
    ]


# ============================================================================
# 预算估算
# ============================================================================


def estimate_budgets(
    request: TravelRequest,
    days: list[DayPlan],
) -> tuple[list[DayPlan], BudgetBreakdown]:
    """先计算每日预算，再逐项汇总为全程预算。

    高德 POI 不提供统一的门票和住宿报价，因此这里使用固定经验参数，并在
    最终结果中明确标记为估算值，避免将其误解为供应商实时报价。
    """

    other_costs = _split_amount(request.travelers * OTHER_RATE, len(days))
    daily = [
        _estimate_day_budget(request, day, other)
        for day, other in zip(days, other_costs, strict=True)
    ]
    budgeted_days = [
        day.model_copy(update={"budget": budget}) for day, budget in zip(days, daily, strict=True)
    ]
    return budgeted_days, _sum_budgets(daily)


def _apply_selected_costs(
    days: list[DayPlan],
    budget: BudgetBreakdown,
    request: TravelRequest,
    intercity: IntercityPlan | None,
    accommodation: AccommodationPlan | None,
) -> tuple[list[DayPlan], BudgetBreakdown]:
    """用已选真实报价替换经验住宿费，并同步每日与全程预算。"""

    if intercity is None and accommodation is None:
        return days, budget
    outbound, return_trip = _rail_costs(intercity, request.travelers)
    rail = outbound + return_trip
    hotel = _hotel_total(accommodation)
    nightly = _split_amount(hotel, max(1, len(days) - 1)) if hotel else []
    adjusted: list[DayPlan] = []
    for index, day in enumerate(days):
        if day.budget is None:
            adjusted.append(day)
            continue
        rail_cost = (outbound if index == 0 else 0) + (
            return_trip if index == len(days) - 1 else 0
        )
        hotel_cost = nightly[index] if index < len(nightly) else 0.0
        old = day.budget
        transport = round(old.transport + rail_cost, 2)
        total = round(old.total - old.hotel + hotel_cost + rail_cost, 2)
        updated = old.model_copy(
            update={
                "transport": transport,
                "local_transport": old.transport,
                "intercity_transport": rail_cost if rail_cost else None,
                "hotel": hotel_cost,
                "total": total,
            }
        )
        adjusted.append(day.model_copy(update={"budget": updated}))
    summed = _sum_budgets([day.budget for day in adjusted if day.budget])
    summed = summed.model_copy(
        update={
            "local_transport": budget.transport,
            "intercity_transport": rail if rail else None,
        }
    )
    return adjusted, summed


def _rail_costs(intercity: IntercityPlan | None, travelers: int) -> tuple[float, float]:
    if intercity is None:
        return 0.0, 0.0
    outbound = intercity.outbound.price_from if intercity.outbound else None
    return_trip = intercity.return_trip.price_from if intercity.return_trip else None
    return (
        round((outbound or 0) * travelers, 2),
        round((return_trip or 0) * travelers, 2),
    )


def _hotel_total(accommodation: AccommodationPlan | None) -> float:
    if not accommodation or not accommodation.hotel or accommodation.self_arranged:
        return 0.0
    hotel = accommodation.hotel
    if hotel.total_price is not None:
        return round(hotel.total_price, 2)
    if hotel.price_per_night is not None:
        return round(hotel.price_per_night * accommodation.nights, 2)
    return 0.0


def _edited_spot(item: DayActivityEdit, candidates: CandidateCatalog) -> SpotPlan:
    poi = candidates.find(item.poi_id)
    if poi is None or poi.category != "attraction":
        raise AppError("invalid_poi", "编辑只能使用候选列表中的真实景点", 422)
    return SpotPlan(
        poi_id=poi.id,
        name=poi.name,
        address=poi.address,
        latitude=poi.latitude,
        longitude=poi.longitude,
        start_time=item.start_time,
        duration_minutes=item.duration_minutes,
        note="用户调整后的安排",
        image_url=poi.image_url,
    )


async def _edited_routes(
    provider: MapProvider,
    city: City,
    pois: list[Poi],
    mode: str,
) -> list[RouteSegment]:
    """编辑后优先复用异步交通能力，旧 Provider 则在线程中逐段查询。"""

    import asyncio

    operation = getattr(provider, "get_transport_async", None)
    if operation is not None:
        result = cast(TransportResult, await operation(city, pois, mode))
        return result.routes
    routes: list[RouteSegment] = []
    for left, right in zip(pois, pois[1:], strict=False):
        routes.append(await asyncio.to_thread(provider.get_route, left, right))
    return routes


def _estimate_day_budget(
    request: TravelRequest,
    day: DayPlan,
    other: float,
) -> BudgetBreakdown:
    distance = sum(route.distance_km for route in day.routes)
    transport = max(DAILY_TRANSPORT_MINIMUM, distance * DISTANCE_RATE)
    hotel = HOTEL_RATES[request.hotel_level] if day.hotel is not None else 0.0
    meals = request.travelers * 3 * MEAL_RATE
    tickets = len(day.activities) * request.travelers * TICKET_RATE
    total = transport + hotel + meals + tickets + other
    return BudgetBreakdown(
        transport=round(transport, 2),
        hotel=round(hotel, 2),
        meals=round(meals, 2),
        tickets=round(tickets, 2),
        other=round(other, 2),
        total=round(total, 2),
    )


def _split_amount(total: float, parts: int) -> list[float]:
    """平均拆分金额，并由最后一天吸收小数舍入差额。"""

    base = round(total / parts, 2)
    return [base] * (parts - 1) + [round(total - base * (parts - 1), 2)]


def _sum_budgets(budgets: list[BudgetBreakdown]) -> BudgetBreakdown:
    fields = ("transport", "hotel", "meals", "tickets", "other")
    values = {field: round(sum(getattr(item, field) for item in budgets), 2) for field in fields}
    return BudgetBreakdown(**values, total=round(sum(values.values()), 2))


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
        if day.budget:
            lines.append(_daily_budget_line(day.budget))
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
    lines: list[str] = []
    for item in routes:
        source = item.source.provider if item.source else "unknown"
        line = (
            f"- 路线：{item.from_poi_id} → {item.to_poi_id}，{item.mode}，"
            f"约 {item.distance_km} 公里 / {item.duration_minutes} 分钟，来源：{source}"
        )
        lines.append(line)
        lines.extend(
            f"  - {transit.name}：{transit.departure_stop} → {transit.arrival_stop}"
            for transit in item.transit_lines
        )
    return lines


def _daily_budget_line(budget: BudgetBreakdown) -> str:
    return (
        f"- 当日预算：交通 ¥{budget.transport:.0f} / 住宿 ¥{budget.hotel:.0f} / "
        f"餐饮 ¥{budget.meals:.0f} / 门票 ¥{budget.tickets:.0f} / "
        f"其他 ¥{budget.other:.0f}，合计 ¥{budget.total:.0f}（估算）"
    )


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
    source = weather.source.provider if weather.source else "unknown"
    return f"天气：{values[0] or '未知'}，白天 {values[1] or '未知'}℃，来源：{source}"
