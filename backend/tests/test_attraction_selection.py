"""景点选择事实边界测试。"""

from datetime import date

import pytest

from openzltravel.domain.errors import FactBoundaryError, TravelGraphError
from openzltravel.domain.models import (
    ActivityDraft,
    CandidateCatalog,
    DayDraft,
    ItineraryDraft,
    Poi,
    RailChoice,
    RailOption,
    TravelFacts,
    TravelRequirements,
    TravelSelection,
)
from openzltravel.domain.validation import validate_draft, validate_selection


def _facts() -> TravelFacts:
    attractions = [
        Poi(
            id=f"a{index}",
            name=f"景点{index}",
            category="attraction",
            latitude=30 + index / 100,
            longitude=120 + index / 100,
        )
        for index in range(1, 6)
    ]
    return TravelFacts(
        catalog=CandidateCatalog(attractions=attractions, required_attraction_ids=["a1"])
    )


def _selection(attraction_ids: list[str]) -> TravelSelection:
    return TravelSelection(
        attraction_ids=attraction_ids,
        self_arranged_outbound=True,
        self_arranged_return=True,
    )


def _requirements() -> TravelRequirements:
    return TravelRequirements(
        origin="杭州", destination="北京", start_date=date(2026, 9, 1), trip_days=1
    )


def test_attraction_selection_rejects_unknown_fact_id() -> None:
    with pytest.raises(FactBoundaryError, match="不存在的景点"):
        validate_selection(_requirements(), _facts(), _selection(["a1", "unknown"]))


def test_attraction_selection_rejects_daily_capacity_overflow() -> None:
    with pytest.raises(TravelGraphError, match="最多选择 4 个"):
        validate_selection(_requirements(), _facts(), _selection(["a1", "a2", "a3", "a4", "a5"]))


def test_attraction_selection_must_keep_named_required_places() -> None:
    with pytest.raises(FactBoundaryError, match="必去"):
        validate_selection(_requirements(), _facts(), _selection(["a2"]))


def test_attraction_selection_respects_selected_train_time_capacity() -> None:
    travel_date = date(2026, 9, 1)
    facts = _facts().model_copy(
        update={
            "outbound_options": [
                RailOption(
                    option_id="late",
                    direction="outbound",
                    travel_date=travel_date,
                    train_code="G1",
                    from_station="杭州",
                    to_station="北京",
                    departure_time="14:00",
                    arrival_time="18:00",
                )
            ],
            "return_options": [
                RailOption(
                    option_id="night",
                    direction="return",
                    travel_date=travel_date,
                    train_code="G2",
                    from_station="北京",
                    to_station="杭州",
                    departure_time="20:00",
                    arrival_time="23:00",
                )
            ],
        }
    )
    selection = TravelSelection(
        attraction_ids=["a1"],
        outbound=RailChoice(option_id="late"),
        return_trip=RailChoice(option_id="night"),
    )

    with pytest.raises(TravelGraphError, match="当前车次最多安排 0 个景点"):
        validate_selection(_requirements(), facts, selection)


def test_final_draft_rejects_selection_catalog_state_mismatch() -> None:
    facts = _facts().model_copy(
        update={
            "catalog": CandidateCatalog(
                attractions=_facts().catalog.attractions[:2],
                required_attraction_ids=["a1"],
            )
        }
    )
    draft = ItineraryDraft(
        summary="错误状态",
        days=[
            DayDraft(
                day_index=1,
                theme="景点",
                activities=[
                    ActivityDraft(poi_id="a1", start_time="09:00"),
                    ActivityDraft(poi_id="a2", start_time="12:00"),
                ],
            )
        ],
    )

    with pytest.raises(FactBoundaryError, match="景点选择状态不一致"):
        validate_draft(_requirements(), facts, _selection(["a1", "a2"]), draft)
