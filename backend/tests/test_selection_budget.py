"""选择阶段预算预览的确定性测试。"""

from datetime import date
from typing import Literal

from domain.models import (
    HotelOption,
    RailChoice,
    RailOption,
    RailSeat,
    TravelFacts,
    TravelRequirements,
    TravelSelection,
)
from domain.planning import calculate_selection_budget_preview


def _rail(
    option_id: str,
    direction: Literal["outbound", "return"],
    price: float | None,
) -> RailOption:
    return RailOption(
        option_id=option_id,
        direction=direction,
        travel_date=date(2026, 9, 1),
        train_code="G1",
        from_station="上海",
        to_station="西安",
        departure_time="08:00",
        arrival_time="12:00",
        seats=[RailSeat(name="二等座", price=price)],
        price_from=price,
    )


def test_selection_budget_preview_marks_known_over_budget_total() -> None:
    requirements = TravelRequirements(
        origin="上海",
        destination="西安",
        start_date=date(2026, 9, 1),
        trip_days=2,
        travelers=2,
        budget=1500,
    )
    facts = TravelFacts(
        outbound_options=[_rail("out", "outbound", 100)],
        return_options=[_rail("back", "return", 120)],
        hotel_options=[HotelOption(hotel_id="h1", name="酒店", total_price=600)],
    )
    selection = TravelSelection(
        attraction_ids=["a1", "a2"],
        outbound=RailChoice(option_id="out", seat_type="二等座"),
        return_trip=RailChoice(option_id="back", seat_type="二等座"),
        hotel_id="h1",
    )

    preview = calculate_selection_budget_preview(requirements, facts, selection)

    assert preview.intercity_transport == 440
    assert preview.hotel == 600
    assert preview.estimated_total == 1600
    assert preview.estimated_remaining == -100
    assert preview.is_over_budget is True
    assert preview.unknown_costs == ["市内交通"]


def test_selection_budget_preview_keeps_unknown_prices_explicit() -> None:
    requirements = TravelRequirements(
        origin="上海",
        destination="西安",
        start_date=date(2026, 9, 1),
        trip_days=2,
        budget=5000,
    )
    preview = calculate_selection_budget_preview(
        requirements,
        TravelFacts(),
        TravelSelection(
            attraction_ids=["a1"],
            self_arranged_outbound=True,
            self_arranged_return=True,
            self_arranged_hotel=True,
        ),
    )

    assert preview.intercity_transport is None
    assert preview.hotel is None
    assert preview.unknown_costs == ["去程车票", "返程车票", "住宿", "市内交通"]
    assert preview.is_over_budget is False
