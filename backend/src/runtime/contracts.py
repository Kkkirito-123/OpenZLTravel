"""TravelGraph 与外部实现之间的最小依赖契约。

本模块位于领域层与实现层之间：图节点只依赖这些 Protocol，Provider 和模型网关负责
实现它们。把契约放在顶层可以避免 ``graph`` 与 ``providers`` 互相导入，也让依赖方向
保持为 ``domain <- contracts <- graph/providers``。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from pydantic import BaseModel

from domain.models import (
    CandidateCatalog,
    City,
    DestinationCandidate,
    HotelOption,
    Poi,
    RailOption,
    RouteSegment,
    TravelRequirements,
    WeatherDay,
)

ModelMessage = dict[str, str]


class StructuredModel(Protocol):
    """可生成 Pydantic 结构化结果的异步模型网关。

    Graph 只依赖这个 Protocol，而不依赖 OpenAI SDK 的具体类。这样学习和测试时可以
    注入 FakeModel，生产环境再由 ``model_gateway.py`` 提供真实实现。
    """

    async def ainvoke(
        self,
        messages: Sequence[ModelMessage],
        *,
        response_model: type[BaseModel],
        max_tokens: int,
    ) -> BaseModel | dict[str, Any]:
        """调用模型；实现层应使用真正的异步 HTTP 连接池。"""


class CatalogGateway(Protocol):
    """城市解析、POI 查询与确定性目的地推荐的端口。

    CatalogGateway 返回的是事实模型，不返回原始 HTTP JSON。Provider 负责解析和生成
    稳定 ID，图节点只关心这些稳定的领域对象。
    """

    async def resolve_city(self, destination: str) -> City:
        """把城市名称解析为真实城市事实。"""

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
    """12306 车次查询的端口。

    ``bool`` 返回值只是缓存命中信息，不参与业务路由；即使缓存命中，选项仍必须经过
    同一套事实校验。
    """

    async def search(
        self,
        origin: str,
        destination: str,
        travel_date: date,
        direction: str,
    ) -> tuple[list[RailOption], bool]:
        """返回直达或已组合的候选车次与缓存命中标志。"""


class HotelGateway(Protocol):
    """实时酒店与本地目录降级接口的端口。

    返回的第三项是面向用户的降级说明，允许实时酒店失败时仍展示目录候选或自行安排。
    """

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
class TravelDependencies:
    """TravelGraph 的唯一依赖容器。

    这是依赖注入的学习入口：图节点接收一个容器，却不知道具体使用高德、12306 还是
    Fake Provider。``container.py`` 负责组装，测试可以直接构造本类替换任意一项。
    """

    # 事实来源端口；具体实现由 runtime.container 装配。
    catalog: CatalogGateway
    # 去程、返程车次查询端口。
    rail: RailGateway
    # 实时或目录酒店查询端口。
    hotels: HotelGateway
    # 日期范围天气查询端口。
    weather: WeatherGateway
    # 确定性路线查询端口；失败时只产生 warning。
    routes: RouteGateway
    # 三个可选模型；为 None 时各 Agent 走确定性降级。
    requirement_model: StructuredModel | None = None
    planner_model: StructuredModel | None = None
    review_model: StructuredModel | None = None
