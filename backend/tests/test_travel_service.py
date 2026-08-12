import json
import sqlite3
from pathlib import Path

import pytest

from app.errors import AppError, DraftError, ProviderError
from app.models import DraftActivity
from app.storage import SqliteTripRepository
from app.travel import TravelService
from tests.fakes import FakeMapProvider, FakePlanner, sample_draft, sample_request


def make_service(
    tmp_path: Path,
    planner: FakePlanner | None = None,
    map_provider: FakeMapProvider | None = None,
) -> tuple[TravelService, FakeMapProvider]:
    provider = map_provider or FakeMapProvider()
    service = TravelService(
        map_provider=provider,
        planner=planner or FakePlanner(),
        repository=SqliteTripRepository(str(tmp_path / "trips.sqlite3")),
    )
    return service, provider


def test_create_saves_complete_itinerary(tmp_path: Path) -> None:
    service, map_provider = make_service(tmp_path)

    itinerary = service.create(sample_request())

    assert itinerary.destination == "测试市"
    assert len(itinerary.days) == 2
    assert itinerary.days[0].activities[0].poi_id == "a1"
    assert itinerary.days[0].activities[0].image_url.endswith("a1.jpg")
    assert itinerary.days[0].meals[0].image_url.endswith("r1.jpg")
    assert itinerary.days[0].hotel and itinerary.days[0].hotel.image_url.endswith("h1.jpg")
    assert itinerary.days[-1].hotel is None
    assert itinerary.days[0].weather.day_weather == "晴"
    assert itinerary.days[0].budget and itinerary.days[0].budget.total == 1080
    assert itinerary.days[1].budget and itinerary.days[1].budget.total == 660
    assert itinerary.budget.total == 1740
    assert map_provider.route_calls == 0
    assert service.get(itinerary.trip_id).trip_id == itinerary.trip_id


def test_daily_route_cost_and_total_are_derived_from_days(tmp_path: Path) -> None:
    draft = sample_draft()
    draft.days[0].activities.append(
        DraftActivity(poi_id="a2", start_time="13:00", duration_minutes=90)
    )
    provider = FakeMapProvider(route_distance_km=20)
    service, _ = make_service(tmp_path, FakePlanner(draft), provider)

    itinerary = service.create(sample_request())

    assert itinerary.days[0].budget and itinerary.days[0].budget.transport == 50
    assert provider.route_calls == 1
    assert itinerary.budget.total == sum(day.budget.total for day in itinerary.days if day.budget)


def test_route_failure_does_not_save_partial_trip(tmp_path: Path) -> None:
    class FailedRouteProvider(FakeMapProvider):
        def get_route(self, from_poi, to_poi):
            raise ProviderError("route_not_found", "无法获取驾车路线")

    draft = sample_draft()
    draft.days[0].activities.append(DraftActivity(poi_id="a2"))
    service, _ = make_service(tmp_path, FakePlanner(draft), FailedRouteProvider())

    with pytest.raises(ProviderError, match="无法获取驾车路线"):
        service.create(sample_request())
    assert service.list() == []


def test_one_day_trip_has_no_hotel_cost(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path, FakePlanner(sample_draft(1)))

    itinerary = service.create(sample_request(1))

    assert itinerary.days[0].hotel is None
    assert itinerary.days[0].budget and itinerary.days[0].budget.hotel == 0
    assert itinerary.budget.hotel == 0


def test_missing_overnight_hotel_adds_budget_warning(tmp_path: Path) -> None:
    draft = sample_draft()
    draft.days[0].hotel_id = None
    service, _ = make_service(tmp_path, FakePlanner(draft))

    itinerary = service.create(sample_request())

    assert itinerary.days[0].budget and itinerary.days[0].budget.hotel == 0
    assert any("预算未包含该晚住宿费用" in warning for warning in itinerary.warnings)


def test_other_cost_rounding_is_absorbed_by_last_day(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path, FakePlanner(sample_draft(3)))

    itinerary = service.create(sample_request(3))

    other_costs = [day.budget.other for day in itinerary.days if day.budget]
    assert other_costs == [66.67, 66.67, 66.66]
    assert itinerary.budget.other == 200


def test_people_count_scales_people_based_costs(tmp_path: Path) -> None:
    one_person, _ = make_service(tmp_path / "one")
    two_people, _ = make_service(tmp_path / "two")

    one_budget = one_person.create(sample_request().model_copy(update={"travelers": 1})).budget
    two_budget = two_people.create(sample_request()).budget

    assert two_budget.meals == one_budget.meals * 2
    assert two_budget.tickets == one_budget.tickets * 2
    assert two_budget.other == one_budget.other * 2


def test_planner_context_excludes_image_urls() -> None:
    catalog = FakeMapProvider().candidates

    assert all(
        "image_url" not in item for items in catalog.prompt_data().values() for item in items
    )


def test_legacy_itinerary_json_remains_readable(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    repository = SqliteTripRepository(str(database_path))
    service = TravelService(FakeMapProvider(), FakePlanner(), repository)
    itinerary = service.create(sample_request())
    payload = itinerary.model_dump(mode="json")
    for day in payload["days"]:
        day.pop("budget")
        for place in [*day["activities"], *day["meals"]]:
            place.pop("image_url")
        if day["hotel"]:
            day["hotel"].pop("image_url")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE trips SET itinerary_json = ? WHERE trip_id = ?",
            (json.dumps(payload, ensure_ascii=False), str(itinerary.trip_id)),
        )

    restored = repository.get(itinerary.trip_id)

    assert restored and restored.days[0].budget is None
    assert restored.days[0].activities[0].image_url is None


def test_failure_does_not_save(tmp_path: Path) -> None:
    invalid = sample_draft()
    invalid.days[0].activities[0].poi_id = "not-exist"
    service, _ = make_service(tmp_path, FakePlanner(invalid))

    with pytest.raises(DraftError):
        service.create(sample_request())
    assert service.list() == []


def test_model_gets_one_repair_attempt(tmp_path: Path) -> None:
    class RepairPlanner(FakePlanner):
        def __init__(self) -> None:
            super().__init__(sample_draft())

        def plan(self, request, candidates, feedback=None):
            self.feedback.append(feedback)
            if feedback is None:
                raise DraftError("请修正地点 ID")
            return sample_draft()

    planner = RepairPlanner()
    service, _ = make_service(tmp_path, planner)

    itinerary = service.create(sample_request())

    assert itinerary.trip_id
    assert planner.feedback == [None, "请修正地点 ID"]


def test_invalid_destination_range_is_rejected() -> None:
    with pytest.raises(ValueError, match="最多支持 7 天"):
        sample_request(days=8)


def test_missing_trip_uses_stable_error(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)

    with pytest.raises(AppError, match="行程不存在"):
        service.get(__import__("uuid").uuid4())
