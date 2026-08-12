"""测试用的外部服务替身。"""

from datetime import date

from app.models import (
    CandidateCatalog,
    City,
    Coordinate,
    ItineraryDraft,
    Poi,
    RouteSegment,
    TravelRequest,
    WeatherDay,
)


def sample_request(days: int = 2) -> TravelRequest:
    return TravelRequest(
        destination="测试市",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, days),
        travelers=2,
        budget=3000,
        preferences=["自然风景"],
    )


def sample_catalog() -> CandidateCatalog:
    return CandidateCatalog(
        attractions=[
            Poi(
                id="a1",
                name="湖畔公园",
                address="测试市湖畔路 1 号",
                category="attraction",
                latitude=30.1,
                longitude=120.1,
                image_url="https://images.example.com/a1.jpg",
            ),
            Poi(
                id="a2",
                name="古城街区",
                address="测试市古城路 2 号",
                category="attraction",
                latitude=30.2,
                longitude=120.2,
                image_url="https://images.example.com/a2.jpg",
            ),
            Poi(
                id="a3",
                name="山顶观景台",
                address="测试市山路 3 号",
                category="attraction",
                latitude=30.3,
                longitude=120.3,
                image_url="https://images.example.com/a3.jpg",
            ),
        ],
        restaurants=[
            Poi(
                id="r1",
                name="湖畔餐厅",
                address="测试市湖畔路 8 号",
                category="restaurant",
                latitude=30.11,
                longitude=120.11,
                image_url="https://images.example.com/r1.jpg",
            ),
        ],
        hotels=[
            Poi(
                id="h1",
                name="测试酒店",
                address="测试市中心 9 号",
                category="hotel",
                latitude=30.12,
                longitude=120.12,
                image_url="https://images.example.com/h1.jpg",
            ),
        ],
    )


def sample_draft(days: int = 2) -> ItineraryDraft:
    return ItineraryDraft(
        summary="围绕城市自然风景和老街安排的轻松行程。",
        days=[
            {
                "day_index": index,
                "theme": f"第 {index} 天主题",
                "activities": [
                    {
                        "poi_id": f"a{index}",
                        "start_time": "09:00",
                        "duration_minutes": 120,
                        "note": "按节奏游览",
                    }
                ],
                "meal_ids": ["r1"],
                "hotel_id": "h1",
            }
            for index in range(1, days + 1)
        ],
        tips=["根据天气准备衣物"],
    )


class FakeMapProvider:
    def __init__(
        self,
        candidates: CandidateCatalog | None = None,
        route_distance_km: float = 2.5,
    ) -> None:
        self.candidates = candidates or sample_catalog()
        self.route_distance_km = route_distance_km
        self.route_calls = 0

    def resolve_city(self, destination: str) -> City:
        return City(name=destination, adcode="123")

    def search_candidates(self, city: City) -> CandidateCatalog:
        return self.candidates

    def get_weather(self, city: City, start_date: date, end_date: date) -> list[WeatherDay]:
        return [
            WeatherDay(
                date=start_date, day_weather="晴", day_temperature="25", night_temperature="18"
            )
        ]

    def get_route(self, from_poi: Poi, to_poi: Poi) -> RouteSegment:
        self.route_calls += 1
        return RouteSegment(
            from_poi_id=from_poi.id,
            to_poi_id=to_poi.id,
            distance_km=self.route_distance_km,
            duration_minutes=15,
            polyline=[
                Coordinate(latitude=from_poi.latitude, longitude=from_poi.longitude),
                Coordinate(latitude=to_poi.latitude, longitude=to_poi.longitude),
            ],
        )


class FakePlanner:
    def __init__(self, draft: ItineraryDraft | None = None) -> None:
        self.draft = draft or sample_draft()
        self.feedback: list[str | None] = []

    def plan(
        self, request: TravelRequest, candidates: CandidateCatalog, feedback: str | None = None
    ) -> ItineraryDraft:
        self.feedback.append(feedback)
        return self.draft
