"""Benchmark 的固定事实 Fixture；不访问任何外部 Provider。"""

from __future__ import annotations

from datetime import date

from openzltravel.domain.models import (
    CandidateCatalog,
    City,
    DestinationCandidate,
    HotelOption,
    Poi,
    TravelFacts,
    TravelOrder,
    TravelRequirements,
    TravelSelection,
)
from openzltravel.infrastructure.providers.fakes import (
    FakeCatalogProvider,
    FakeHotelProvider,
    FakeRailProvider,
    FakeRouteProvider,
    FakeWeatherProvider,
)
from openzltravel.runtime.contracts import AssistantDependencies, PlanningDependencies


def assistant_dependencies(*, hotel_warning: str | None = None) -> AssistantDependencies:
    city = City(name="杭州", adcode="330100", latitude=30.27, longitude=120.15)
    catalog = CandidateCatalog(
        attractions=[
            Poi(
                id="poi-west-lake",
                name="西湖",
                category="attraction",
                latitude=30.25,
                longitude=120.14,
                address="杭州市西湖区",
                image_url="https://example.test/west-lake.jpg",
            ),
            Poi(
                id="poi-lingyin",
                name="灵隐寺",
                category="attraction",
                latitude=30.24,
                longitude=120.10,
                address="杭州市西湖区灵隐路",
            ),
            Poi(
                id="poi-museum",
                name="杭州城市规划展示馆",
                category="attraction",
                latitude=30.28,
                longitude=120.16,
            ),
        ],
        restaurants=[
            Poi(
                id="poi-food",
                name="杭帮菜馆",
                category="restaurant",
                latitude=30.26,
                longitude=120.15,
            )
        ],
        hotels=[
            Poi(
                id="poi-hotel",
                name="湖滨酒店",
                category="hotel",
                latitude=30.26,
                longitude=120.16,
            )
        ],
    )
    destinations = [
        DestinationCandidate(
            candidate_id="destination-hangzhou",
            city=city,
            score=0.95,
            reasons=["人文与自然景点丰富"],
        )
    ]
    return AssistantDependencies(
        catalog=FakeCatalogProvider(city, catalog, destinations),
        rail=FakeRailProvider(),
        hotels=FakeHotelProvider(
            [HotelOption(hotel_id="hotel-live-1", name="湖滨酒店", total_price=680)],
            warning=hotel_warning,
        ),
        weather=FakeWeatherProvider(),
    )


def planning_dependencies() -> PlanningDependencies:
    return PlanningDependencies(routes=FakeRouteProvider())


def travel_order(fixture: str = "hangzhou_basic") -> TravelOrder:
    if fixture != "hangzhou_basic":
        raise ValueError(f"未知 Graph Fixture: {fixture}")
    attractions = [
        Poi(
            id="poi-west-lake",
            name="西湖",
            category="attraction",
            latitude=30.25,
            longitude=120.14,
        ),
        Poi(
            id="poi-lingyin",
            name="灵隐寺",
            category="attraction",
            latitude=30.24,
            longitude=120.10,
        ),
        Poi(
            id="poi-museum",
            name="杭州城市规划展示馆",
            category="attraction",
            latitude=30.28,
            longitude=120.16,
        ),
    ]
    return TravelOrder(
        requirements=TravelRequirements(
            origin="上海",
            destination="杭州",
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 2),
            budget=5000,
        ),
        facts=TravelFacts(
            city=City(name="杭州", adcode="330100", latitude=30.27, longitude=120.15),
            catalog=CandidateCatalog(
                attractions=attractions,
                required_attraction_ids=[item.id for item in attractions],
            ),
        ),
        selection=TravelSelection(
            attraction_ids=[item.id for item in attractions],
            self_arranged_outbound=True,
            self_arranged_return=True,
            self_arranged_hotel=True,
        ),
    )
