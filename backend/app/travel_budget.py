"""OpenZLTravel 的预算估算与真实报价合并规则。

本模块只处理金额和预算提示，不访问供应商、数据库或模型。固定经验参数必须在
界面和导出结果中标记为估算；车票、酒店存在真实报价时才覆盖对应估算项。
"""

from app.models import (
    AccommodationPlan,
    BudgetBreakdown,
    DayPlan,
    HotelPlan,
    IntercityPlan,
    TravelRequest,
)

HOTEL_RATES = {"经济": 220.0, "舒适": 420.0, "品质": 760.0}
TICKET_RATE = 80.0
MEAL_RATE = 60.0
DAILY_TRANSPORT_MINIMUM = 40.0
DISTANCE_RATE = 2.5
OTHER_RATE = 100.0


def budget_warnings(
    request: TravelRequest,
    days: list[DayPlan],
    accommodation: AccommodationPlan | None = None,
) -> list[str]:
    """返回未纳入预算的住宿夜提示。"""

    if accommodation and accommodation.self_arranged:
        return ["住宿由用户自行安排，预算未包含住宿费用。"]
    if accommodation and accommodation.hotel:
        return []
    return [
        f"{day.date.isoformat()} 未安排住宿，预算未包含该晚住宿费用"
        for day in days
        if day.date < request.end_date and day.hotel is None
    ]


def budget_limit_warning(
    request: TravelRequest,
    budget: BudgetBreakdown,
) -> str | None:
    """在真实车票、酒店报价合并后判断用户预算上限。"""

    if request.budget and budget.total > request.budget:
        return f"预估总预算 ¥{budget.total:.0f} 高于你的预算上限"
    return None


def replace_budget_limit_warning(
    warnings: list[str],
    request: TravelRequest,
    budget: BudgetBreakdown,
) -> list[str]:
    """局部编辑重算后同步预算提示，避免旧金额残留或重复追加。"""

    retained = [item for item in warnings if not item.startswith("预估总预算 ¥")]
    limit_warning = budget_limit_warning(request, budget)
    return [*retained, limit_warning] if limit_warning else retained


def apply_accommodation(
    days: list[DayPlan],
    accommodation: AccommodationPlan | None,
    hotel_level: str,
) -> list[DayPlan]:
    """把已选酒店投影到住宿夜；缺少坐标时仅保留顶部住宿事实。"""

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


def estimate_budgets(
    request: TravelRequest,
    days: list[DayPlan],
) -> tuple[list[DayPlan], BudgetBreakdown]:
    """先计算每日预算，再逐项汇总为全程预算。"""

    # POI 没有统一实时门票和住宿报价，固定经验参数只能作为估算使用。
    other_costs = _split_amount(request.travelers * OTHER_RATE, len(days))
    daily = [
        _estimate_day_budget(request, day, other)
        for day, other in zip(days, other_costs, strict=True)
    ]
    budgeted_days = [
        day.model_copy(update={"budget": budget}) for day, budget in zip(days, daily, strict=True)
    ]
    return budgeted_days, _sum_budgets(daily)


def apply_selected_costs(
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
        rail_cost = (outbound if index == 0 else 0) + (return_trip if index == len(days) - 1 else 0)
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
