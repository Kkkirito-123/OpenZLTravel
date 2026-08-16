"""OpenZLTravel 的只读行程导出。

导出层仅把已经校验并保存的领域模型序列化为 Markdown，不重算路线、天气或预算，
避免下载内容与页面显示出现两套事实。
"""

from app.models import BudgetBreakdown, Itinerary, RouteSegment, SpotPlan, WeatherDay


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
        lines.append(
            f"- 路线：{item.from_poi_id} → {item.to_poi_id}，{item.mode}，"
            f"约 {item.distance_km} 公里 / {item.duration_minutes} 分钟，来源：{source}"
        )
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
