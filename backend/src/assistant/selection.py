"""Assistant 决策和卡片动作的统一事实校验路径。

本模块不做自然语言解析。自然语言由 LLM 输出结构化 ID，卡片由前端直接提交 ID；两者
最终都必须在当前签名快照的候选集合中查找。任何未知 POI、车次或酒店 ID 都不能写入
``AssistantSnapshot``，从而阻断伪造事实进入 TravelGraph。
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from assistant.models import AssistantAction, AssistantDecision, AssistantSnapshot
from domain.errors import TravelGraphError
from domain.models import (
    RailChoice,
    RequirementPatch,
    TravelFacts,
    TravelRequirements,
    TravelSelection,
)
from domain.validation import validate_selection


def apply_action(snapshot: AssistantSnapshot, action: AssistantAction) -> str:  # noqa: C901
    """应用卡片操作；所有 ID 都必须来自签名快照。"""

    if action.kind == "select_destination":
        candidate = next(
            (
                item
                for item in snapshot.destination_candidates
                if item.candidate_id == action.candidate_id
            ),
            None,
        )
        if candidate is None:
            raise ValueError("目的地候选不存在")
        snapshot.requirements = snapshot.requirements.model_copy(
            update={"destination": candidate.city.name, "region": None}
        )
        snapshot.facts = TravelFacts(city=candidate.city)
        snapshot.selection = TravelSelection()
        return f"我选择{candidate.city.name}。"
    if action.kind == "select_attractions":
        known = (
            {item.id for item in snapshot.facts.catalog.attractions}
            if snapshot.facts.catalog
            else set()
        )
        if not action.attraction_ids or set(action.attraction_ids) - known:
            raise ValueError("景点选择包含未知 ID")
        snapshot.selection = snapshot.selection.model_copy(
            update={"attraction_ids": action.attraction_ids}
        )
        return "我选择这些景点。"
    if action.kind in {"select_outbound", "select_return"}:
        return _apply_rail_action(snapshot, action)
    if action.kind == "select_hotel":
        hotel = next(
            (item for item in snapshot.facts.hotel_options if item.hotel_id == action.hotel_id),
            None,
        )
        if hotel is None:
            raise ValueError("酒店候选不存在")
        snapshot.selection = snapshot.selection.model_copy(
            update={"hotel_id": hotel.hotel_id, "self_arranged_hotel": False}
        )
        return f"我选择{hotel.name}。"
    assert action.kind == "self_arrange" and action.target is not None
    field = {"outbound": "outbound", "return": "return_trip", "hotel": "hotel_id"}[
        action.target
    ]
    snapshot.selection = snapshot.selection.model_copy(
        update={f"self_arranged_{action.target}": True, field: None}
    )
    return f"{action.target}由我自行安排。"


def merge_requirements(
    current: TravelRequirements,
    patch: RequirementPatch,
    *,
    allow_clear: bool = False,
) -> TravelRequirements:
    """只合并 LLM 明确返回的字段，日期变更时清理派生结束日期。"""

    base = current.model_dump()
    incoming = (
        patch.model_dump(exclude_unset=True)
        if allow_clear
        else patch.model_dump(exclude_none=True)
    )
    if "start_date" in incoming or "trip_days" in incoming:
        if "end_date" not in incoming:
            base["end_date"] = None
    if "end_date" in incoming and "trip_days" not in incoming:
        base["trip_days"] = None
    base.update(incoming)
    return TravelRequirements.model_validate(base)


def apply_decision(snapshot: AssistantSnapshot, decision: AssistantDecision) -> None:
    """合并 Agent 输出，但丢弃不存在的事实 ID。"""

    try:
        snapshot.requirements = merge_requirements(snapshot.requirements, decision.patch)
    except ValidationError:
        pass
    updates: dict[str, Any] = {}
    known_attractions = (
        {item.id for item in snapshot.facts.catalog.attractions}
        if snapshot.facts.catalog
        else set()
    )
    if decision.attraction_ids and not set(decision.attraction_ids) - known_attractions:
        updates["attraction_ids"] = decision.attraction_ids
    _apply_rail_decision(snapshot, decision, updates)
    if decision.hotel_id in {item.hotel_id for item in snapshot.facts.hotel_options}:
        updates.update(hotel_id=decision.hotel_id, self_arranged_hotel=False)
    for flag, field in (
        ("self_arranged_outbound", "outbound"),
        ("self_arranged_return", "return_trip"),
        ("self_arranged_hotel", "hotel_id"),
    ):
        if getattr(decision, flag):
            updates[flag] = True
            updates[field] = None
    snapshot.selection = snapshot.selection.model_copy(update=updates)


def travel_choices_complete(snapshot: AssistantSnapshot) -> bool:
    """判断景点、往返交通和住宿选择是否已经完整。"""

    selection = snapshot.selection
    requirements = snapshot.requirements
    rail_complete = bool(selection.outbound or selection.self_arranged_outbound) and bool(
        selection.return_trip or selection.self_arranged_return
    )
    hotel_complete = requirements.days_count <= 1 or bool(
        selection.hotel_id or selection.self_arranged_hotel
    )
    return bool(selection.attraction_ids) and rail_complete and hotel_complete


def update_status(snapshot: AssistantSnapshot) -> None:
    """根据需求、事实和选择派生会话状态。"""

    missing = snapshot.requirements.missing_fields()
    if snapshot.requirements.budget is None:
        missing.append("budget")
    if missing or not travel_choices_complete(snapshot):
        snapshot.status = "collecting"
        return
    try:
        validate_selection(snapshot.requirements, snapshot.facts, snapshot.selection)
    except TravelGraphError:
        snapshot.status = "collecting"
        return
    snapshot.status = "ready"


def _apply_rail_action(snapshot: AssistantSnapshot, action: AssistantAction) -> str:
    direction = "outbound" if action.kind == "select_outbound" else "return"
    options = (
        snapshot.facts.outbound_options
        if direction == "outbound"
        else snapshot.facts.return_options
    )
    option = next((item for item in options if item.option_id == action.option_id), None)
    if option is None:
        raise ValueError("车次候选不存在")
    choice = RailChoice(option_id=option.option_id, seat_type=action.seat_type)
    field = "outbound" if direction == "outbound" else "return_trip"
    flag = "self_arranged_outbound" if direction == "outbound" else "self_arranged_return"
    snapshot.selection = snapshot.selection.model_copy(update={field: choice, flag: False})
    label = "去程" if direction == "outbound" else "返程"
    return f"我选择{option.train_code}作为{label}。"


def _apply_rail_decision(
    snapshot: AssistantSnapshot,
    decision: AssistantDecision,
    updates: dict[str, Any],
) -> None:
    pairs = (
        (
            decision.outbound_option_id,
            decision.outbound_seat_type,
            snapshot.facts.outbound_options,
            "outbound",
            "self_arranged_outbound",
        ),
        (
            decision.return_option_id,
            decision.return_seat_type,
            snapshot.facts.return_options,
            "return_trip",
            "self_arranged_return",
        ),
    )
    for option_id, seat_type, options, field, flag in pairs:
        if option_id and option_id in {item.option_id for item in options}:
            updates[field] = RailChoice(option_id=option_id, seat_type=seat_type)
            updates[flag] = False
