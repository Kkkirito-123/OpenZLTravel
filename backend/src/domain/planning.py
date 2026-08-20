"""不依赖 LLM 的行程降级、预算和事实查找规则。

这里的函数必须保持可重现：同一份需求、事实和用户选择应得到同一份草稿/预算，便于
Planner 超时后的降级、Checkpoint 重放以及单元测试比较结果。
"""

from __future__ import annotations

from collections.abc import Sequence

from domain.models import (
    ActivityDraft,
    BudgetBreakdown,
    DayDraft,
    HotelOption,
    ItineraryDraft,
    Poi,
    RailChoice,
    RailOption,
    TravelFacts,
    TravelRequirements,
    TravelSelection,
)

_PACE_CAPACITY = {"轻松": 2, "适中": 3, "紧凑": 4}


def deterministic_draft(
    requirements: TravelRequirements,
    facts: TravelFacts,
    selection: TravelSelection,
) -> ItineraryDraft:
    """当 PlannerAgent 不可用时，按稳定顺序生成可验证草稿。

    该函数不是“第二个 Agent”：它不理解自然语言，只按 Catalog 顺序、节奏容量和
    已验证的车次时间限制分配真实 POI，因此输出可以直接接受同一套事实边界校验。
    """

    if facts.catalog is None or requirements.destination is None:
        raise ValueError("确定性规划需要目的地与 POI 事实")
    days_count = requirements.days_count
    capacities = [_PACE_CAPACITY[requirements.pace]] * days_count
    _apply_rail_time_limits(capacities, selection, facts)
    attractions = facts.catalog.attractions[: sum(capacities)]
    buckets = _split_by_capacity(attractions, capacities)
    hotel_id = None if selection.self_arranged_hotel else selection.hotel_id
    days = [
        _build_day(index, pois, facts, hotel_id, days_count)
        for index, pois in enumerate(buckets, start=1)
    ]
    return ItineraryDraft(
        summary=f"已根据真实候选地点整理 {requirements.destination} 行程。",
        days=days,
        tips=["车次、房价和天气会变化，出发前请再次确认。"],
    )


def calculate_budget(
    requirements: TravelRequirements,
    facts: TravelFacts,
    selection: TravelSelection,
    draft: ItineraryDraft,
) -> BudgetBreakdown:
    """优先使用真实报价，只对餐饮与门票给出明示经验估算。

    缺少实时票价或房价时对应字段保持 ``None``；只有明确标为估算的餐饮和门票才会
    进入汇总，避免把经验数字伪装成 Provider 事实。
    """

    outbound = _rail_price(selection.outbound, facts.outbound_options)
    return_price = _rail_price(selection.return_trip, facts.return_options)
    known_rail = [value for value in (outbound, return_price) if value is not None]
    intercity = sum(known_rail) * requirements.travelers if known_rail else None
    hotel = _selected_hotel(selection, facts.hotel_options)
    hotel_total = _hotel_price(hotel, requirements.days_count)
    local_values = [route.cost for routes in facts.routes.values() for route in routes]
    known_local = [value for value in local_values if value is not None]
    local = sum(known_local) if known_local else None
    activities = sum(len(day.activities) for day in draft.days)
    meals = 80.0 * requirements.travelers * requirements.days_count
    tickets = 60.0 * requirements.travelers * activities
    total = sum(value for value in (intercity, hotel_total, local, meals, tickets) if value)
    return BudgetBreakdown(
        intercity_transport=intercity,
        local_transport=local,
        hotel=hotel_total,
        meals_estimated=round(meals, 2),
        tickets_estimated=round(tickets, 2),
        total_known=round(total, 2),
    )


def selected_rail(choice: RailChoice | None, options: list[RailOption]) -> RailOption | None:
    """按用户选择查找一条车次事实。"""

    if choice is None:
        return None
    return next((item for item in options if item.option_id == choice.option_id), None)


def _apply_rail_time_limits(
    capacities: list[int],
    selection: TravelSelection,
    facts: TravelFacts,
) -> None:
    outbound = selected_rail(selection.outbound, facts.outbound_options)
    returning = selected_rail(selection.return_trip, facts.return_options)
    if outbound and _hour(outbound.arrival_time) >= 14:
        capacities[0] = 0 if _hour(outbound.arrival_time) >= 18 else 1
    if returning and _hour(returning.departure_time) <= 14:
        capacities[-1] = 0 if _hour(returning.departure_time) <= 10 else 1


def _split_by_capacity(pois: list[Poi], capacities: list[int]) -> list[list[Poi]]:
    buckets: list[list[Poi]] = []
    offset = 0
    for capacity in capacities:
        buckets.append(pois[offset : offset + capacity])
        offset += capacity
    return buckets


def _build_day(
    day_index: int,
    pois: Sequence[Poi],
    facts: TravelFacts,
    hotel_id: str | None,
    days_count: int,
) -> DayDraft:
    activities = [
        ActivityDraft(
            poi_id=str(poi.id),
            start_time=f"{9 + offset * 3:02d}:00",
            duration_minutes=120,
            note="按候选顺序游览，减少不必要的折返。",
        )
        for offset, poi in enumerate(pois)
    ]
    restaurants = facts.catalog.restaurants if facts.catalog else []
    meal_ids = [item.id for item in restaurants[:2]]
    theme = "抵达与安顿" if not pois else " · ".join(poi.name for poi in pois[:2])
    return DayDraft(
        day_index=day_index,
        theme=theme,
        activities=activities,
        meal_ids=meal_ids,
        hotel_id=hotel_id if day_index < days_count else None,
        notes=[] if pois else ["当天不强行安排额外景点。"],
    )


def _rail_price(choice: RailChoice | None, options: list[RailOption]) -> float | None:
    option = selected_rail(choice, options)
    if option is None:
        return None
    if choice and choice.seat_type:
        seat = next((item for item in option.seats if item.name == choice.seat_type), None)
        return seat.price if seat else None
    return option.price_from


def _selected_hotel(
    selection: TravelSelection,
    options: list[HotelOption],
) -> HotelOption | None:
    if selection.self_arranged_hotel or not selection.hotel_id:
        return None
    return next((item for item in options if item.hotel_id == selection.hotel_id), None)


def _hotel_price(hotel: HotelOption | None, days_count: int) -> float | None:
    if hotel is None:
        return None
    if hotel.total_price is not None:
        return hotel.total_price
    if hotel.price_per_night is not None:
        return hotel.price_per_night * max(0, days_count - 1)
    return None


def _hour(value: str) -> int:
    try:
        return int(value.split(":", 1)[0])
    except (TypeError, ValueError):
        return 12
