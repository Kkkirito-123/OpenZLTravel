"""OpenZLTravel 当前架构的稳定领域类型与确定性规则。

这里导出的模型是 Graph、Provider 和 HTTP 展示层共享的事实语言；它们不包含网络客户端、
会话持久化或 Agent 调用，因此可以被确定性校验与离线测试直接复用。
"""

from domain.models import (
    BudgetBreakdown,
    CandidateCatalog,
    City,
    DayDraft,
    DestinationCandidate,
    HotelOption,
    ItineraryDraft,
    PlaceSnapshot,
    Poi,
    RailChoice,
    RailOption,
    RailSeat,
    RequirementPatch,
    ReviewIssue,
    ReviewResult,
    RouteSegment,
    TravelFacts,
    TravelRequirements,
    TravelSelection,
    TripRecord,
    WeatherDay,
)

__all__ = [
    "BudgetBreakdown",
    "CandidateCatalog",
    "City",
    "DayDraft",
    "DestinationCandidate",
    "HotelOption",
    "ItineraryDraft",
    "PlaceSnapshot",
    "Poi",
    "RailChoice",
    "RailOption",
    "RailSeat",
    "RequirementPatch",
    "ReviewIssue",
    "ReviewResult",
    "RouteSegment",
    "TravelFacts",
    "TravelRequirements",
    "TravelSelection",
    "TripRecord",
    "WeatherDay",
]
