"""最终保存前的确定性事实与结构校验。

校验函数只接受领域模型，不信任 LLM、浏览器或 Provider 的原始字典。它负责确认选择的
每个 ID 都存在于工单事实、席别和时间可安排、草稿中的景点来源合法、路线端点与草稿
顺序一致。校验失败使用稳定的领域错误码，供 Assistant、Graph 和前端分别处理。
"""

from __future__ import annotations

from domain.errors import FactBoundaryError, TravelGraphError
from domain.models import (
    ItineraryDraft,
    RailChoice,
    RailOption,
    TravelFacts,
    TravelRequirements,
    TravelSelection,
)


def validate_selection(
    requirements: TravelRequirements,
    facts: TravelFacts,
    selection: TravelSelection,
) -> None:
    """验证用户选择能否在当前事实和需求下进入规划。"""

    _validate_choice(
        selection.outbound.option_id if selection.outbound else None,
        selection.self_arranged_outbound,
        {item.option_id for item in facts.outbound_options},
        "outbound",
    )
    _validate_seat(selection.outbound, facts.outbound_options, "outbound")
    _validate_choice(
        selection.return_trip.option_id if selection.return_trip else None,
        selection.self_arranged_return,
        {item.option_id for item in facts.return_options},
        "return",
    )
    _validate_seat(selection.return_trip, facts.return_options, "return")
    if requirements.days_count <= 1:
        if selection.hotel_id or selection.self_arranged_hotel:
            raise TravelGraphError(
                "hotel_not_required",
                "一日游不接受酒店选择或自行安排住宿标志",
            )
    else:
        _validate_choice(
            selection.hotel_id,
            selection.self_arranged_hotel,
            {item.hotel_id for item in facts.hotel_options},
            "hotel",
        )
    _validate_attractions(requirements, facts, selection)
    _validate_schedule_capacity(requirements, facts, selection)


def _validate_attractions(
    requirements: TravelRequirements, facts: TravelFacts, selection: TravelSelection
) -> None:
    catalog = facts.catalog
    if catalog is None:
        raise TravelGraphError("catalog_missing", "景点选择前缺少地点目录")
    selected = selection.attraction_ids
    if not selected:
        raise TravelGraphError("attraction_selection_required", "请至少选择一个景点")
    if len(selected) != len(set(selected)):
        raise TravelGraphError("duplicate_attraction_id", "景点选择不能包含重复项")
    capacity = requirements.days_count * 4
    if len(selected) > capacity:
        raise TravelGraphError("attraction_capacity_exceeded", f"最多选择 {capacity} 个景点")
    known = {item.id for item in catalog.attractions}
    if unknown := sorted(set(selected) - known):
        raise FactBoundaryError("unknown_attraction_id", f"不存在的景点: {', '.join(unknown)}")
    if missing := sorted(set(catalog.required_attraction_ids) - set(selected)):
        raise FactBoundaryError("required_attraction_missing", f"必去项缺失: {', '.join(missing)}")


def _validate_schedule_capacity(
    requirements: TravelRequirements,
    facts: TravelFacts,
    selection: TravelSelection,
) -> None:
    capacity = requirements.days_count * 4
    first_day = _selected_option(selection.outbound, facts.outbound_options)
    last_day = _selected_option(selection.return_trip, facts.return_options)
    if first_day is not None:
        capacity -= 4 - _day_capacity(_minutes(first_day.arrival_time), 18 * 60)
    if last_day is not None:
        capacity -= 4 - _day_capacity(9 * 60, _minutes(last_day.departure_time))
    if first_day is not None and last_day is not None and requirements.days_count == 1:
        capacity = _day_capacity(
            _minutes(first_day.arrival_time), _minutes(last_day.departure_time)
        )
    if len(selection.attraction_ids) > capacity:
        raise TravelGraphError(
            "attraction_schedule_capacity",
            f"当前车次最多安排 {max(capacity, 0)} 个景点",
        )


def _day_capacity(start_minutes: int, end_minutes: int) -> int:
    if end_minutes - start_minutes <= 120:
        return 0
    return min(4, (end_minutes - start_minutes) // 120)


def _selected_option(choice: RailChoice | None, options: list[RailOption]) -> RailOption | None:
    return next(
        (item for item in options if choice and item.option_id == choice.option_id), None
    )


def _minutes(value: str) -> int:
    try:
        hour, minute = map(int, value.split(":"))
        return hour * 60 + minute
    except (TypeError, ValueError):
        return 0


def validate_draft(
    requirements: TravelRequirements,
    facts: TravelFacts,
    selection: TravelSelection,
    draft: ItineraryDraft,
) -> None:
    """验证确定性草稿没有超出景点、天数、时间和住宿约束。"""

    if len(draft.days) != requirements.days_count:
        raise TravelGraphError("invalid_day_count", "行程天数与用户需求不一致")
    expected = list(range(1, requirements.days_count + 1))
    if [day.day_index for day in draft.days] != expected:
        raise TravelGraphError("invalid_day_order", "行程 day_index 必须连续且从 1 开始")
    _validate_draft_hotels(requirements, selection, draft)
    if facts.catalog is None:
        raise TravelGraphError("catalog_missing", "最终校验缺少地点目录")
    if set(facts.catalog.required_attraction_ids) != set(selection.attraction_ids):
        raise FactBoundaryError("selection_catalog_mismatch", "景点选择状态不一致")
    attraction_ids = {item.id for item in facts.catalog.attractions}
    restaurant_ids = {item.id for item in facts.catalog.restaurants}
    hotel_ids = {
        *(item.id for item in facts.catalog.hotels),
        *(item.hotel_id for item in facts.hotel_options),
    }
    referenced_attractions = {
        activity.poi_id for day in draft.days for activity in day.activities
    }
    referenced_meals = {meal_id for day in draft.days for meal_id in day.meal_ids}
    referenced_hotels = {day.hotel_id for day in draft.days if day.hotel_id}
    unknown = sorted(
        (referenced_attractions - attraction_ids)
        | (referenced_meals - restaurant_ids)
        | (referenced_hotels - hotel_ids)
    )
    if unknown:
        raise FactBoundaryError(
            "unknown_fact_id",
            f"规划结果引用了不存在的事实 ID: {', '.join(unknown)}",
        )
    missing_required = sorted(
        set(facts.catalog.required_attraction_ids) - referenced_attractions
    )
    if missing_required:
        raise FactBoundaryError(
            "requested_place_missing",
            f"行程遗漏了用户确认的必去地点: {', '.join(missing_required)}",
        )


def _validate_draft_hotels(
    requirements: TravelRequirements,
    selection: TravelSelection,
    draft: ItineraryDraft,
) -> None:
    for day in draft.days:
        requires_night = requirements.days_count > 1 and day.day_index < requirements.days_count
        expected_hotel = (
            selection.hotel_id
            if requires_night and not selection.self_arranged_hotel
            else None
        )
        if day.hotel_id != expected_hotel:
            raise FactBoundaryError(
                "hotel_selection_mismatch",
                f"第 {day.day_index} 天酒店安排与用户选择不一致",
            )


def validate_routes(facts: TravelFacts, draft: ItineraryDraft) -> None:
    """验证路线只连接相邻真实 POI，并与每日草稿顺序保持一致。"""

    if facts.catalog is None:
        return
    known = {item.id for item in facts.catalog.all}
    unknown = sorted(
        {
            endpoint
            for routes in facts.routes.values()
            for route in routes
            for endpoint in (route.from_poi_id, route.to_poi_id)
            if endpoint not in known
        }
    )
    if unknown:
        raise FactBoundaryError(
            "unknown_route_endpoint",
            f"路线引用了不存在的 POI ID: {', '.join(unknown)}",
        )
    day_pairs = {
        day.day_index: list(zip(
            [activity.poi_id for activity in day.activities],
            [activity.poi_id for activity in day.activities][1:],
            strict=False,
        ))
        for day in draft.days
    }
    for day_index, routes in facts.routes.items():
        actual = [(route.from_poi_id, route.to_poi_id) for route in routes]
        if actual and actual != day_pairs.get(day_index, []):
            raise FactBoundaryError(
                "route_order_mismatch",
                f"第 {day_index} 天路线未按相邻景点逐段连接",
            )


def _validate_choice(
    selected_id: str | None,
    self_arranged: bool,
    valid_ids: set[str],
    field: str,
) -> None:
    if self_arranged:
        if selected_id:
            raise TravelGraphError("selection_conflict", f"{field} 不能同时选择方案和自行安排")
        return
    if not selected_id:
        raise TravelGraphError("selection_required", f"{field} 必须选择方案或自行安排")
    if selected_id not in valid_ids:
        raise FactBoundaryError("unknown_selection_id", f"{field} 引用了不存在的选项")


def _validate_seat(
    choice: RailChoice | None,
    options: list[RailOption],
    field: str,
) -> None:
    if choice is None or not choice.seat_type:
        return
    option = next(
        (item for item in options if item.option_id == choice.option_id),
        None,
    )
    seats = option.seats if option else []
    if choice.seat_type not in {item.name for item in seats}:
        raise FactBoundaryError("unknown_seat_type", f"{field} 引用了不存在的席别")
