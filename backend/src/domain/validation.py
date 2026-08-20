"""最终保存前的确定性事实与结构校验。"""

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
    """选择必须引用 Provider 返回的 ID，或显式声明自行安排。

    这是第一个事实边界：用户从 interrupt 恢复的 JSON 仍然是不可信输入，只有命中当前
    Provider 事实集合后，才允许进入 Planner。
    """

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
        return
    _validate_choice(
        selection.hotel_id,
        selection.self_arranged_hotel,
        {item.hotel_id for item in facts.hotel_options},
        "hotel",
    )


def validate_draft(
    requirements: TravelRequirements,
    facts: TravelFacts,
    selection: TravelSelection,
    draft: ItineraryDraft,
) -> None:
    """验证日期、用户选择和所有事实 ID，阻止 Agent 编造或替换事实。

    Planner 输出即使符合 Pydantic 结构，也可能引用不存在的 POI 或另一家酒店；因此
    结构校验和事实校验必须分开执行，且保存节点只能位于本函数之后。
    """

    if len(draft.days) != requirements.days_count:
        raise TravelGraphError("invalid_day_count", "行程天数与用户需求不一致")
    expected = list(range(1, requirements.days_count + 1))
    if [day.day_index for day in draft.days] != expected:
        raise TravelGraphError("invalid_day_order", "行程 day_index 必须连续且从 1 开始")
    _validate_draft_hotels(requirements, selection, draft)
    if facts.catalog is None:
        raise TravelGraphError("catalog_missing", "最终校验缺少地点目录")
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
            f"Agent 引用了不存在的事实 ID: {', '.join(unknown)}",
        )


def _validate_draft_hotels(
    requirements: TravelRequirements,
    selection: TravelSelection,
    draft: ItineraryDraft,
) -> None:
    """酒店安排必须逐晚服从用户选择，最后一天和一日游都不能虚构额外住宿。"""

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


def validate_routes(facts: TravelFacts) -> None:
    """路线端点也必须属于同一个真实 POI 候选池。

    路线由独立 Provider 产生，不能因为端点看起来像字符串就默认可信；未知端点会在
    Store 写入前失败，避免历史行程保存无法解释的路线事实。
    """

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
