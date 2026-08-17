"""OpenZLTravel 的核心旅行流程。

本文件只编排行程事实、校验模型草稿、读写行程和局部编辑。预算规则放在
``travel_budget``，Markdown 导出放在 ``travel_export``；外部请求交给 providers，
持久化交给 storage，不在这里处理 HTTP 响应。
"""

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import cast
from uuid import UUID, uuid4

from app.errors import AppError, ConflictError, DraftError, ResourceNotFoundError
from app.models import (
    AccommodationPlan,
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
from app.storage import UNSCOPED_VISITOR_ID, TripRepository
from app.travel_budget import (
    apply_accommodation,
    apply_selected_costs,
    budget_limit_warning,
    budget_warnings,
    estimate_budgets,
    replace_budget_limit_warning,
)
from app.travel_export import itinerary_to_markdown

__all__ = ["TravelService", "itinerary_to_markdown"]


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

    def create(
        self, request: TravelRequest, visitor_id: UUID = UNSCOPED_VISITOR_ID
    ) -> Itinerary:
        """生成并保存一份经过事实校验的完整行程。"""

        city = self.map_provider.resolve_city(request.destination)
        candidates = self.map_provider.search_candidates(city)
        if not candidates.attractions:
            raise AppError("no_attractions", f"暂时找不到“{city.name}”的可用景点数据", 422)
        weather = self.map_provider.get_weather(city, request.start_date, request.end_date)
        draft = self._plan_with_one_repair(request, candidates)
        itinerary = self._build_itinerary(request, city, draft, candidates, weather, None)
        # 所有外部查询和业务校验都成功后才保存，避免历史记录出现半成品。
        self.repository.save(itinerary, request, visitor_id)
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
        raw_days = apply_accommodation(raw_days, accommodation, request.hotel_level)
        days, budget = estimate_budgets(request, raw_days)
        warnings = _weather_warnings(request, weather)
        warnings.extend(budget_warnings(request, days, accommodation))
        warnings.extend(transport_warnings or [])
        days, budget = apply_selected_costs(days, budget, request, intercity, accommodation)
        limit_warning = budget_limit_warning(request, budget)
        if limit_warning:
            warnings.append(limit_warning)
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

    async def create_async(
        self, request: TravelRequest, visitor_id: UUID = UNSCOPED_VISITOR_ID
    ) -> Itinerary:
        """通过 LangGraph 编排异步生成；测试替身仍兼容旧同步接口。"""

        if not hasattr(self.map_provider, "get_transport_async"):
            import asyncio

            return await asyncio.to_thread(self.create, request, visitor_id)
        from app.workflow import TravelWorkflow

        itinerary = await TravelWorkflow(self).run(request)
        # 只有图中所有事实和规则校验完成后才写入一次，避免半成品历史记录。
        self.repository.save(itinerary, request, visitor_id)
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
            transport.routes if transport is not None else self._get_routes(activities, candidates)
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

    def get(
        self, trip_id: UUID, visitor_id: UUID = UNSCOPED_VISITOR_ID
    ) -> Itinerary:
        """读取完整行程，不存在时抛出稳定业务错误。"""

        itinerary = self.repository.get(trip_id, visitor_id)
        if itinerary is None:
            raise ResourceNotFoundError("行程不存在")
        return itinerary

    def list(self, visitor_id: UUID = UNSCOPED_VISITOR_ID) -> list[TripSummary]:
        """返回历史行程摘要。"""

        return self.repository.list(visitor_id)

    def delete(self, trip_id: UUID, visitor_id: UUID = UNSCOPED_VISITOR_ID) -> None:
        """删除指定行程，不存在时抛出稳定业务错误。"""

        if not self.repository.delete(trip_id, visitor_id):
            raise ResourceNotFoundError("行程不存在")

    def alternatives(
        self,
        trip_id: UUID,
        candidates: CandidateCatalog,
        visitor_id: UUID = UNSCOPED_VISITOR_ID,
    ) -> TripAlternatives:
        """返回编辑时允许引用的真实候选景点。"""

        itinerary = self.get(trip_id, visitor_id)
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
        visitor_id: UUID = UNSCOPED_VISITOR_ID,
    ) -> Itinerary:
        """编辑一天并只重算该日路线与全程预算。"""

        itinerary = self.get(trip_id, visitor_id)
        request = self.repository.get_request(trip_id, visitor_id)
        if request is None:
            raise ResourceNotFoundError("行程不存在")
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
        budgeted_days, budget = apply_selected_costs(
            budgeted_days,
            budget,
            request,
            itinerary.intercity,
            itinerary.accommodation,
        )
        updated = itinerary.model_copy(
            update={
                "days": budgeted_days,
                "budget": budget,
                "revision": itinerary.revision + 1,
                "warnings": replace_budget_limit_warning(itinerary.warnings, request, budget),
            }
        )
        if not self.repository.save_if_revision(
            updated,
            request,
            edit.expected_revision,
            visitor_id,
        ):
            raise ConflictError()
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
