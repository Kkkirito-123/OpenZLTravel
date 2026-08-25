"""TravelGraph 与外部实现之间的最小 Protocol，保持单向依赖。

这些 Protocol 是应用层端口，不包含 HTTP、数据库连接或第三方 SDK 类型。Assistant 只
拿到 Catalog、Rail、Hotel、Weather 四个事实端口；TravelGraph 只拿到 RouteGateway。
``runtime.container`` 在组合根把真实 Provider 或 Fake Provider 注入进去，因此离线
Benchmark 可以完全替换外部世界，而不改变业务节点。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from openzltravel.domain.models import (
    CandidateCatalog,
    City,
    DestinationCandidate,
    HotelOption,
    Poi,
    RailOption,
    ResolvedPlace,
    RouteSegment,
    TravelRequirements,
    WeatherDay,
)


class CatalogGateway(Protocol):
    """城市、POI 与目的地推荐端口；只返回带稳定 ID 的事实模型。"""

    async def resolve_city(self, destination: str) -> City:
        """把城市名称解析为真实城市事实。"""

    async def resolve_place(self, query: str) -> ResolvedPlace:
        """区分规范城市与具体地点，并返回 Provider 确认的所属城市/POI。"""

    async def search_candidates(self, city: City) -> CandidateCatalog:
        """返回真实 POI 候选池。"""

    async def recommend_destinations(
        self,
        origin: str,
        region: str,
        preferences: list[str],
        limit: int = 5,
    ) -> list[DestinationCandidate]:
        """按目录覆盖、标签、距离和配套确定性排名。"""


class RailGateway(Protocol):
    """12306 车次端口；缓存命中标志不参与路由，候选仍走同一事实校验。"""

    async def search(
        self,
        origin: str,
        destination: str,
        travel_date: date,
        direction: str,
    ) -> tuple[list[RailOption], bool]:
        """返回直达或已组合的候选车次与缓存命中标志。"""


class HotelGateway(Protocol):
    """实时酒店与目录降级端口，第三个返回值是面向用户的降级说明。"""

    async def search(
        self,
        requirements: TravelRequirements,
        catalog: CandidateCatalog,
    ) -> tuple[list[HotelOption], bool, str | None]:
        """返回酒店候选、缓存标志和可选降级说明。"""


class WeatherGateway(Protocol):
    """日期范围天气查询的端口。"""

    async def get_weather(
        self,
        city: City,
        start_date: date,
        end_date: date,
    ) -> list[WeatherDay]:
        """返回上游实际覆盖的天气。"""


class RouteGateway(Protocol):
    """相邻真实 POI 之间的路线查询端口。"""

    async def get_routes(
        self,
        city: City,
        day_pois: Sequence[Poi],
        mode: str,
    ) -> tuple[list[RouteSegment], list[str]]:
        """返回一天的路线与明确降级警告。"""


@dataclass(frozen=True)
class AssistantDependencies:
    """交流助手可调用的只读事实端口。

    Assistant 可以查询和筛选事实，但不能通过这些端口保存最终行程，也不能获得
    TravelGraph 的 Checkpoint。
    """

    catalog: CatalogGateway
    rail: RailGateway
    hotels: HotelGateway
    weather: WeatherGateway


@dataclass(frozen=True)
class PlanningDependencies:
    """TravelGraph 唯一外部依赖。

    事实发现已经由 Assistant 完成，Graph 只在每日顺序生成后查询相邻 POI 路线。这个
    依赖集合故意不包含 Catalog、铁路、酒店和天气，防止规划阶段重新越过工单边界。
    """

    routes: RouteGateway
