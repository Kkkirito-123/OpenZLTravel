from pathlib import Path

import pytest

from app.errors import AppError, DraftError
from app.storage import SqliteTripRepository
from app.travel import TravelService
from tests.fakes import FakeMapProvider, FakePlanner, sample_draft, sample_request


def make_service(
    tmp_path: Path,
    planner: FakePlanner | None = None,
) -> tuple[TravelService, FakeMapProvider]:
    map_provider = FakeMapProvider()
    service = TravelService(
        map_provider=map_provider,
        planner=planner or FakePlanner(),
        repository=SqliteTripRepository(str(tmp_path / "trips.sqlite3")),
    )
    return service, map_provider


def test_create_saves_complete_itinerary(tmp_path: Path) -> None:
    service, map_provider = make_service(tmp_path)

    itinerary = service.create(sample_request())

    assert itinerary.destination == "测试市"
    assert len(itinerary.days) == 2
    assert itinerary.days[0].activities[0].poi_id == "a1"
    assert itinerary.days[0].weather.day_weather == "晴"
    assert itinerary.budget.total > 0
    assert map_provider.route_calls == 0
    assert service.get(itinerary.trip_id).trip_id == itinerary.trip_id


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
