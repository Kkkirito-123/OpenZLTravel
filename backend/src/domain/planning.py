"""可重现的行程、预算和事实规则。

本模块是纯领域服务：输入是已验证的需求、事实和选择，输出是可序列化的草稿、预算或
查询结果。它不访问网络、不读取环境变量，也不依赖 LangGraph，因此同一个工单在重试、
Checkpoint 恢复和固定 Benchmark 中应得到一致结果。路线是唯一例外：路线数据属于实时
Provider，必须由 ``travel_graph.nodes.planning`` 在顺序确定后查询。
"""

from __future__ import annotations

import re
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
    SelectionBudgetPreview,
    TravelFacts,
    TravelRequirements,
    TravelSelection,
)

_PACE_CAPACITY = {"轻松": 2, "适中": 3, "紧凑": 4}


def deterministic_draft(
    requirements: TravelRequirements,
    facts: TravelFacts,
    selection: TravelSelection,
    instruction: str | None = None,
) -> ItineraryDraft:
    """按节奏容量、车次时间和用户修改指令分配真实 POI。

    该函数不做推荐，也不创建新的 POI。它只从工单携带的候选中排序和分日；如果用户
    要求移动景点或减少某天安排，函数会重新计算后续草稿，确保路线和预算不会沿用旧结果。
    """

    if facts.catalog is None or requirements.destination is None:
        raise ValueError("确定性规划需要目的地与 POI 事实")
    days_count = requirements.days_count
    capacities = [_PACE_CAPACITY[requirements.pace]] * days_count
    _apply_rail_time_limits(capacities, selection, facts)
    _apply_capacity_revision(capacities, instruction)
    _ensure_required_capacity(capacities, len(facts.catalog.required_attraction_ids))
    attractions = _apply_order_revision(
        facts.catalog.attractions[: sum(capacities)], capacities, instruction
    )
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


def _apply_capacity_revision(capacities: list[int], instruction: str | None) -> None:
    """处理“第 N 天少一个”等明确调整；总容量保持不变，避免漏掉已选景点。"""

    if not instruction or len(capacities) < 2:
        return
    match = re.search(r"第\s*([一二三四五六七1-7])\s*天.*?(?:少安排|少放|少一个)", instruction)
    if match is None:
        return
    target = _day_number(match.group(1)) - 1
    if target < 0 or target >= len(capacities) or capacities[target] <= 0:
        return
    recipient = next(
        (index for index in range(target + 1, len(capacities)) if capacities[index] < 4),
        next((index for index in range(target) if capacities[index] < 4), None),
    )
    if recipient is None:
        return
    capacities[target] -= 1
    capacities[recipient] += 1


def _apply_order_revision(
    attractions: list[Poi], capacities: list[int], instruction: str | None
) -> list[Poi]:
    """把明确点名的真实景点移动到指定日期，其余候选保持原有稳定顺序。"""

    if not instruction:
        return attractions
    match = re.search(r"(.{1,30}?)放(?:到|在)?第\s*([一二三四五六七1-7])\s*天", instruction)
    if match is None:
        return attractions
    day_index = _day_number(match.group(2)) - 1
    if day_index < 0 or day_index >= len(capacities):
        return attractions
    phrase = match.group(1).strip("，。；、把将请 ")
    poi = next(
        (item for item in attractions if item.name in phrase or phrase.endswith(item.name)),
        None,
    )
    if poi is None:
        return attractions
    buckets = _split_by_capacity(attractions, capacities)
    source_index = next(
        (
            index
            for index, bucket in enumerate(buckets)
            if any(item.id == poi.id for item in bucket)
        ),
        None,
    )
    if source_index is None or source_index == day_index:
        return attractions
    source = buckets[source_index]
    source.remove(poi)
    target = buckets[day_index]
    if len(target) >= 4:
        source.append(target.pop())
    target.insert(0, poi)
    capacities[:] = [len(bucket) for bucket in buckets]
    return [item for bucket in buckets for item in bucket]


def _day_number(value: str) -> int:
    return {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7}.get(
        value, int(value) if value.isdigit() else 0
    )


def calculate_budget(
    requirements: TravelRequirements,
    facts: TravelFacts,
    selection: TravelSelection,
    draft: ItineraryDraft,
) -> BudgetBreakdown:
    """优先使用真实报价；未知票价/房价为 None，只有餐饮和门票使用明示估算。"""

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


def calculate_selection_budget_preview(
    requirements: TravelRequirements,
    facts: TravelFacts,
    selection: TravelSelection,
) -> SelectionBudgetPreview:
    """仅使用已选报价和明示估算生成选择阶段预算。"""

    outbound = _rail_price(selection.outbound, facts.outbound_options)
    returning = _rail_price(selection.return_trip, facts.return_options)
    rail_prices = [value for value in (outbound, returning) if value is not None]
    intercity = sum(rail_prices) * requirements.travelers if rail_prices else None
    hotel = _hotel_price(
        _selected_hotel(selection, facts.hotel_options), requirements.days_count
    )
    meals = 80.0 * requirements.travelers * requirements.days_count
    tickets = 60.0 * requirements.travelers * len(selection.attraction_ids)
    total = sum(value for value in (intercity, hotel, meals, tickets) if value is not None)
    remaining = requirements.budget - total if requirements.budget is not None else None
    return SelectionBudgetPreview(
        budget_limit=requirements.budget,
        intercity_transport=intercity,
        hotel=hotel,
        meals_estimated=round(meals, 2),
        tickets_estimated=round(tickets, 2),
        estimated_total=round(total, 2),
        estimated_remaining=round(remaining, 2) if remaining is not None else None,
        unknown_costs=_selection_unknown_costs(requirements, facts, selection),
        is_over_budget=remaining is not None and remaining < 0,
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


def _ensure_required_capacity(capacities: list[int], required_count: int) -> None:
    """用户确认景点优先于节奏降级，但每天仍不超过领域模型的四项上限。"""

    deficit = required_count - sum(capacities)
    for index in sorted(range(len(capacities)), key=capacities.__getitem__, reverse=True):
        added = min(max(0, 4 - capacities[index]), max(0, deficit))
        capacities[index] += added
        deficit -= added
        if deficit <= 0:
            return


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


def _selection_unknown_costs(
    requirements: TravelRequirements,
    facts: TravelFacts,
    selection: TravelSelection,
) -> list[str]:
    unknown: list[str] = []
    legs = (
        ("去程车票", selection.outbound, selection.self_arranged_outbound, facts.outbound_options),
        ("返程车票", selection.return_trip, selection.self_arranged_return, facts.return_options),
    )
    for label, choice, self_arranged, options in legs:
        if self_arranged or choice is None or _rail_price(choice, options) is None:
            unknown.append(label)
    if requirements.days_count > 1:
        selected_hotel = _selected_hotel(selection, facts.hotel_options)
        hotel_price = _hotel_price(selected_hotel, requirements.days_count)
        if selection.self_arranged_hotel or hotel_price is None:
            unknown.append("住宿")
    unknown.append("市内交通")
    return unknown


def _hour(value: str) -> int:
    try:
        return int(value.split(":", 1)[0])
    except (TypeError, ValueError):
        return 12
