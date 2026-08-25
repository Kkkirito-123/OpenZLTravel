"""路线预览进入最终报告前的事实边界测试。"""

import pytest

from domain.errors import FactBoundaryError
from domain.models import (
    ActivityDraft,
    CandidateCatalog,
    DayDraft,
    ItineraryDraft,
    Poi,
    RouteSegment,
    TravelFacts,
)
from domain.validation import validate_routes


def _poi(poi_id: str) -> Poi:
    return Poi(
        id=poi_id,
        name=poi_id,
        category="attraction",
        latitude=30,
        longitude=120,
    )


def _draft() -> ItineraryDraft:
    return ItineraryDraft(
        summary="路线草稿",
        days=[
            DayDraft(
                day_index=1,
                theme="第一天",
                activities=[
                    ActivityDraft(poi_id="a1", start_time="09:00"),
                    ActivityDraft(poi_id="a2", start_time="12:00"),
                    ActivityDraft(poi_id="a3", start_time="15:00"),
                ],
            )
        ],
    )


def _facts(routes: list[RouteSegment]) -> TravelFacts:
    return TravelFacts(
        catalog=CandidateCatalog(attractions=[_poi("a1"), _poi("a2"), _poi("a3")]),
        routes={1: routes},
    )


def _route(left: str, right: str) -> RouteSegment:
    return RouteSegment(
        from_poi_id=left,
        to_poi_id=right,
        distance_km=1,
        duration_minutes=10,
        mode="walk",
    )


def test_routes_must_follow_the_draft_activity_order() -> None:
    with pytest.raises(FactBoundaryError, match="相邻景点"):
        validate_routes(_facts([_route("a2", "a1")]), _draft())


def test_routes_must_connect_each_adjacent_activity() -> None:
    validate_routes(_facts([_route("a1", "a2"), _route("a2", "a3")]), _draft())


def test_waypoint_summary_cannot_replace_adjacent_segments() -> None:
    with pytest.raises(FactBoundaryError, match="相邻景点"):
        validate_routes(_facts([_route("a1", "a3")]), _draft())


def test_empty_degraded_route_preview_remains_allowed() -> None:
    validate_routes(_facts([]), _draft())
