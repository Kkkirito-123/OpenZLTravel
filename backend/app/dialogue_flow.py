"""旅行助手的快速解析、状态合并与确定性 Flow。

本文件只操作结构化用户需求。它不调用 LLM、对话存储或旅行供应商，因此每一步追问与
规划会话创建条件都可在离线测试中验证。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, cast

from pydantic import ValidationError

from app.dialogue_commands import (
    SLOT_NAMES,
    CancelFlowCommand,
    CityResolver,
    ClearSlotCommand,
    CommandEffects,
    CommandState,
    ConfirmCommand,
    DialogueDecision,
    ForgetMemoryCommand,
    MemorySlotName,
    RememberSlotCommand,
    RouteToChatCommand,
    SetSlotCommand,
    SlotName,
    StartFlowCommand,
    TravelCommand,
    TravelCommandBatch,
)
from app.errors import AppError
from app.models import (
    PlanningRequest,
    SlotMetadata,
    TravelDialogueSlots,
    TravelDialogueState,
    TravelMemory,
)


def parse_fast_commands(
    message: str,
    state: TravelDialogueState,
    *,
    today: date | None = None,
) -> TravelCommandBatch | None:
    """只在整条消息可以无歧义消费时跳过模型。"""

    normalized = message.strip()
    compact = re.sub(r"[\s，,。.!！?？]", "", normalized)
    if compact in {"确认", "确定", "可以", "好", "好的"}:
        return TravelCommandBatch(commands=[ConfirmCommand(type="confirm")])
    if compact in {"取消", "算了", "不规划了", "停止"}:
        return TravelCommandBatch(commands=[CancelFlowCommand(type="cancel_flow")])
    if memory_command := _parse_fast_memory(normalized, compact):
        return TravelCommandBatch(commands=[memory_command])
    if not state.active_flow or not state.pending_slots:
        return None

    remaining = normalized
    commands: list[TravelCommand] = []
    for raw_name in state.pending_slots:
        if raw_name not in SLOT_NAMES:
            continue
        name = cast(SlotName, raw_name)
        parsed = _parse_pending_slot(name, remaining, today or date.today())
        if parsed is None:
            continue
        value, start, end = parsed
        commands.append(SetSlotCommand(type="set_slot", name=name, value=value))
        remaining = remaining[:start] + " " * (end - start) + remaining[end:]
    residue = re.sub(r"[\s，,。.!！?？、]", "", remaining)
    residue = re.sub(r"(?:大概|左右|差不多|计划|预算|玩|从|到|至|开始|结束|吧)", "", residue)
    return TravelCommandBatch(commands=commands) if commands and not residue else None


def validate_memory_commands(message: str, batch: TravelCommandBatch) -> None:
    """拒绝模型在没有用户明确授权时写入或删除长期记忆。"""

    has_remember = any(isinstance(item, RememberSlotCommand) for item in batch.commands)
    has_forget = any(isinstance(item, ForgetMemoryCommand) for item in batch.commands)
    if has_remember and not re.search(r"记住|以后都|下次默认|长期偏好", message):
        raise AppError("intent_invalid_output", "只有明确说“记住”时才会保存长期偏好", 422)
    if has_forget and not re.search(r"忘记|不要记|清除|删除", message):
        raise AppError("intent_invalid_output", "只有明确要求忘记时才会删除长期偏好", 422)


def apply_memory_defaults(
    state: TravelDialogueState, memories: tuple[TravelMemory, ...]
) -> TravelDialogueState:
    """把长期偏好作为低优先级默认值放入新会话，不覆盖现有槽位。"""

    slots = state.slots
    metadata = dict(state.slot_metadata)
    defaults = TravelDialogueSlots()
    for memory in memories:
        current = getattr(slots, memory.key)
        if memory.key in metadata or current != getattr(defaults, memory.key):
            continue
        try:
            slots = _replace_slot(slots, memory.key, memory.value)
        except ValidationError:
            continue
        metadata[memory.key] = SlotMetadata(source="memory", updated_turn=0)
    return state.model_copy(update={"slots": slots, "slot_metadata": metadata})


def apply_commands(
    state: TravelDialogueState,
    batch: TravelCommandBatch,
    city_resolver: CityResolver,
    *,
    now: datetime | None = None,
) -> tuple[TravelDialogueState, CommandEffects]:
    """校验命令并生成下一版本状态，不写数据库。"""

    current_time = now or datetime.now(timezone.utc)
    next_revision = state.revision + 1
    command_state = CommandState(
        revision=next_revision,
        slots=state.slots.model_copy(deep=True),
        metadata=dict(state.slot_metadata),
        active_flow=state.active_flow,
        status=state.status,
        changed=set(),
        memory_upserts={},
        memory_deletes=set(),
    )
    for command in batch.commands:
        _apply_command(command_state, command, city_resolver)

    slots, metadata = _derive_dates(
        command_state.slots,
        command_state.metadata,
        command_state.changed,
        next_revision,
    )
    updated = state.model_copy(
        update={
            "revision": next_revision,
            "status": command_state.status,
            "active_flow": command_state.active_flow,
            "slots": slots,
            "slot_metadata": metadata,
            "updated_at": current_time,
        }
    )
    return updated, CommandEffects(
        route_to_chat=command_state.route_to_chat,
        confirmed=command_state.confirmed,
        cancelled=command_state.cancelled,
        validation_message=command_state.validation_message,
        memory_upserts=command_state.memory_upserts,
        memory_deletes=frozenset(command_state.memory_deletes or set()),
    )


def decide_next(state: TravelDialogueState, effects: CommandEffects) -> DialogueDecision:
    """根据结构化状态决定追问、等待推荐或启动规划。"""

    if effects.cancelled:
        decision = _decision(state, "已取消当前旅行需求。", ())
    elif effects.validation_message:
        decision = _decision(state, effects.validation_message, tuple(state.pending_slots))
    elif effects.route_to_chat:
        decision = DialogueDecision(
            state=state,
            reply="我目前可以帮你收集目的地偏好，或创建包含车票、酒店和路线的旅行计划。",
            missing_slots=tuple(state.pending_slots),
        )
    elif state.active_flow is None:
        decision = _decision(
            state,
            "你想先探索适合的目的地，还是已经有具体城市需要规划？",
            (),
        )
    elif state.active_flow == "destination_discovery":
        decision = _discovery_decision(state)
    else:
        decision = _planning_decision(state)
    return _with_memory_ack(decision, effects)


def _apply_command(
    state: CommandState,
    command: TravelCommand,
    city_resolver: CityResolver,
) -> None:
    """把一条受限命令应用到临时状态。"""

    if isinstance(command, StartFlowCommand):
        state.active_flow, state.status = command.flow, "collecting"
        return
    if isinstance(
        command,
        (SetSlotCommand, ClearSlotCommand, RememberSlotCommand, ForgetMemoryCommand),
    ):
        _apply_slot_command(state, command, city_resolver)
        return
    if isinstance(command, ConfirmCommand):
        state.confirmed = True
        return
    if isinstance(command, CancelFlowCommand):
        state.active_flow, state.status, state.cancelled = None, "closed", True
        return
    state.route_to_chat = isinstance(command, RouteToChatCommand)


def _apply_slot_command(
    state: CommandState,
    command: SetSlotCommand | ClearSlotCommand | RememberSlotCommand | ForgetMemoryCommand,
    city_resolver: CityResolver,
) -> None:
    """集中处理槽位与长期偏好命令，主分发函数保持可读。"""

    if isinstance(command, SetSlotCommand):
        _set_slot(state, command, city_resolver)
        return
    if isinstance(command, ClearSlotCommand):
        _clear_slot(state, command.name)
        return
    if isinstance(command, RememberSlotCommand):
        _remember_slot(state, command, city_resolver)
        return
    _forget_memory(state, command.name)


def _clear_slot(state: CommandState, name: SlotName) -> None:
    default = getattr(TravelDialogueSlots(), name)
    state.slots = _replace_slot(state.slots, name, default)
    state.metadata.pop(name, None)
    state.changed.add(name)


def _remember_slot(
    state: CommandState,
    command: RememberSlotCommand,
    city_resolver: CityResolver,
) -> None:
    value = _set_slot(
        state,
        SetSlotCommand(type="set_slot", name=command.name, value=command.value),
        city_resolver,
    )
    if value is None or state.memory_upserts is None:
        return
    state.memory_upserts[command.name] = cast(str | list[str], value)
    if state.memory_deletes is not None:
        state.memory_deletes.discard(command.name)


def _forget_memory(state: CommandState, name: MemorySlotName) -> None:
    if state.memory_deletes is not None:
        state.memory_deletes.add(name)
    if state.memory_upserts is not None:
        state.memory_upserts.pop(name, None)
    metadata = state.metadata.get(name)
    if metadata is None or metadata.source != "memory":
        return
    default = getattr(TravelDialogueSlots(), name)
    state.slots = _replace_slot(state.slots, name, default)
    state.metadata.pop(name, None)
    state.changed.add(name)


def _set_slot(
    state: CommandState,
    command: SetSlotCommand,
    city_resolver: CityResolver,
) -> Any | None:
    """归一化并校验一个用户槽位，非法值不会覆盖原状态。"""

    try:
        value = _normalize_slot(command.name, command.value, city_resolver)
        state.slots = _replace_slot(state.slots, command.name, value)
    except (TypeError, ValueError, ValidationError) as error:
        state.validation_message = _slot_error_message(command.name, error)
        return None
    state.metadata[command.name] = SlotMetadata(source="user_explicit", updated_turn=state.revision)
    state.changed.add(command.name)
    return value


def _replace_slot(slots: TravelDialogueSlots, name: str, value: Any) -> TravelDialogueSlots:
    """通过 Pydantic 重建槽位，避免 model_copy 跳过字段范围校验。"""

    payload = slots.model_dump()
    payload[name] = value
    return TravelDialogueSlots.model_validate(payload)


def _slot_error_message(name: str, error: Exception) -> str:
    """将内部校验细节收敛为可直接展示的中文说明。"""

    if isinstance(error, ValueError) and not isinstance(error, ValidationError):
        return str(error)
    labels = {"days": "天数", "budget": "预算", "travelers": "人数"}
    return f"{labels.get(name, '旅行信息')}不在支持范围内，请重新填写"


def _with_memory_ack(decision: DialogueDecision, effects: CommandEffects) -> DialogueDecision:
    """把记忆变更确认附加到业务回复，避免产生第二次模型调用。"""

    labels = {
        "origin": "常用出发地",
        "preferences": "旅行偏好",
        "dietary_preferences": "饮食偏好",
        "pace": "旅行节奏",
        "hotel_level": "住宿档次",
        "transport_mode": "市内交通偏好",
    }
    saved = [labels[key] for key in (effects.memory_upserts or {})]
    deleted = [labels[key] for key in effects.memory_deletes]
    notices = []
    if saved:
        notices.append(f"已记住：{'、'.join(saved)}。")
    if deleted:
        notices.append(f"已忘记：{'、'.join(deleted)}。")
    if not notices:
        return decision
    return DialogueDecision(
        state=decision.state,
        reply="".join(notices) + decision.reply,
        missing_slots=decision.missing_slots,
        planning_request=decision.planning_request,
    )


def _discovery_decision(state: TravelDialogueState) -> DialogueDecision:
    slots = state.slots
    missing: list[str] = []
    if not (slots.destination_region or slots.destination_city or slots.origin):
        missing.append("destination_region")
    if slots.distance_preference and not slots.origin:
        missing.append("origin")
    if not slots.preferences:
        missing.append("preferences")
    if slots.days is None:
        missing.append("days")
    if slots.budget is None:
        missing.append("budget")
    if missing:
        return _decision(state, _question(missing), tuple(missing))
    ready = state.model_copy(
        update={
            "status": "recommendation_ready",
            "pending_slots": [],
            "last_question": None,
        }
    )
    return _decision(
        ready,
        "需求已经整理完成。当前版本不会凭空推荐城市；你可以补充一个具体城市继续生成完整计划。",
        (),
    )


def _planning_decision(state: TravelDialogueState) -> DialogueDecision:
    slots = state.slots
    missing = [
        name
        for name, value in (
            ("origin", slots.origin),
            ("destination_city", slots.destination_city),
            ("start_date", slots.start_date),
        )
        if value is None
    ]
    if slots.end_date is None and slots.days is None:
        missing.append("days")
    if slots.budget is None:
        missing.append("budget")
    if missing:
        return _decision(state, _question(missing), tuple(missing))
    assert slots.origin and slots.destination_city and slots.start_date and slots.end_date
    request = PlanningRequest(
        origin=slots.origin,
        destination=slots.destination_city,
        start_date=slots.start_date,
        end_date=slots.end_date,
        travelers=slots.travelers,
        budget=slots.budget or 0,
        pace=slots.pace,
        hotel_level=slots.hotel_level,
        transport_mode=slots.transport_mode,
        preferences=slots.preferences,
        dietary_preferences=slots.dietary_preferences,
        notes=slots.notes,
    )
    ready = state.model_copy(update={"pending_slots": [], "last_question": None})
    return DialogueDecision(
        state=ready,
        reply="旅行需求已经完整，正在查询车票、酒店、天气和当地信息。",
        planning_request=request,
    )


def _decision(state: TravelDialogueState, reply: str, missing: tuple[str, ...]) -> DialogueDecision:
    updated = state.model_copy(
        update={"pending_slots": list(missing), "last_question": reply if missing else None}
    )
    return DialogueDecision(updated, reply, missing)


def _normalize_slot(name: str, value: Any, city_resolver: CityResolver) -> Any:
    if name in {"origin", "notes"}:
        return _text(value, name)
    if name == "destination_city":
        return _normalize_city(value, city_resolver)
    if name == "destination_region":
        return _normalize_region(_text(value, name))
    if name in {"start_date", "end_date"}:
        return _normalize_date(value)
    if name in {"days", "travelers"}:
        return int(value)
    if name == "budget":
        return float(value)
    if name in {"preferences", "dietary_preferences"}:
        return _normalize_list(value, name)
    return _normalize_choice(name, value)


def _normalize_city(value: Any, city_resolver: CityResolver) -> str:
    raw = _text(value, "destination_city")
    try:
        return str(city_resolver.resolve_city(raw).name)
    except (AttributeError, LookupError) as error:
        raise ValueError(f"暂时无法确认“{raw}”是具体城市，请换一个城市名称") from error


def _normalize_date(value: Any) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as error:
        raise ValueError("日期需要使用明确的年月日") from error


def _normalize_list(value: Any, name: str) -> list[str]:
    values = value if isinstance(value, list) else [value]
    return list(dict.fromkeys(_text(item, name) for item in values))


def _normalize_choice(name: str, value: Any) -> Any:
    allowed = {
        "distance_preference": {"near", "far"},
        "pace": {"轻松", "适中", "紧凑"},
        "hotel_level": {"经济", "舒适", "品质"},
        "transport_mode": {"auto", "walk", "driving", "transit", "realtime_driving"},
    }
    if name not in allowed or value in allowed[name]:
        return value
    raise ValueError(f"{name} 的取值不受支持")


def _derive_dates(
    slots: TravelDialogueSlots,
    metadata: dict[str, SlotMetadata],
    changed: set[str],
    revision: int,
) -> tuple[TravelDialogueSlots, dict[str, SlotMetadata]]:
    start_date = slots.start_date
    if start_date is None:
        return slots, metadata
    if slots.days is not None and ("days" in changed or slots.end_date is None):
        computed_end = start_date + timedelta(days=slots.days - 1)
        slots = _replace_slot(slots, "end_date", computed_end)
        metadata["end_date"] = SlotMetadata(source="deterministic", updated_turn=revision)
        return slots, metadata
    explicit_end = slots.end_date
    should_derive_days = explicit_end is not None and (
        "end_date" in changed or slots.days is None or "start_date" in changed
    )
    if should_derive_days and explicit_end is not None:
        days = (explicit_end - start_date).days + 1
        if not 1 <= days <= 7:
            raise AppError("dialogue_validation_error", "行程日期必须为 1 到 7 天", 422)
        slots = _replace_slot(slots, "days", days)
        metadata["days"] = SlotMetadata(source="deterministic", updated_turn=revision)
    return slots, metadata


def _question(missing: list[str]) -> str:
    questions = {
        ("days", "budget"): "计划玩几天？大概预算是多少？",
        ("origin", "destination_city"): "你从哪里出发，准备去哪个具体城市？",
        ("start_date", "days"): "计划哪天出发，一共玩几天？",
    }
    if len(missing) >= 2 and (question := questions.get((missing[0], missing[1]))):
        return question
    labels = {
        "origin": "从哪里出发",
        "destination_region": "希望去哪个地区，或者从哪里出发寻找周边目的地",
        "destination_city": "准备去哪个具体城市",
        "start_date": "计划哪天出发",
        "end_date": "计划哪天结束",
        "days": "计划玩几天",
        "budget": "大概预算是多少",
        "preferences": "更喜欢历史、自然、美食还是亲子体验",
    }
    return "，".join(labels.get(item, "还需要补充旅行信息") for item in missing[:2]) + "？"


def _parse_fast_memory(
    message: str, compact: str
) -> RememberSlotCommand | ForgetMemoryCommand | None:
    """确定性处理少量明确记忆语句，复杂表达仍交给受限模型。"""

    aliases: dict[MemorySlotName, tuple[str, ...]] = {
        "origin": ("出发地", "常用出发地"),
        "preferences": ("旅行偏好", "游玩偏好"),
        "dietary_preferences": ("饮食偏好", "口味偏好"),
        "pace": ("旅行节奏", "游玩节奏"),
        "hotel_level": ("住宿档次", "酒店偏好"),
        "transport_mode": ("交通偏好", "市内交通偏好"),
    }
    for key, names in aliases.items():
        if compact in {f"忘记我的{name}" for name in names} | {f"清除我的{name}" for name in names}:
            return ForgetMemoryCommand(type="forget_memory", name=key)

    origin = re.fullmatch(r"记住我(?:通常|默认)?从(?P<value>[^，。]{1,20})出发", compact)
    if origin:
        return RememberSlotCommand(type="remember_slot", name="origin", value=origin.group("value"))
    preference = re.fullmatch(r"记住我(?:喜欢|偏好)(?P<value>[^。]{1,80})", message.strip())
    if preference:
        values = [
            item.strip()
            for item in re.split(r"[、,，]|和", preference.group("value"))
            if item.strip()
        ]
        return RememberSlotCommand(type="remember_slot", name="preferences", value=values)
    return None


def _parse_pending_slot(name: str, content: str, today: date) -> tuple[Any, int, int] | None:
    patterns = {
        "days": re.compile(r"(?P<value>[1-7一二两三四五六七])\s*天"),
        "budget": re.compile(
            r"(?:预算\s*)?(?P<value>\d+(?:\.\d+)?|[一二两三四五六七八九])\s*"
            r"(?P<unit>万|千|k|K)?\s*元?"
        ),
        "travelers": re.compile(r"(?P<value>\d+|[一二两三四五六七八九])\s*(?:人|位)"),
        "start_date": re.compile(
            r"(?:(?P<year>\d{4})[-/年])?(?P<month>\d{1,2})(?:[-/月])(?P<day>\d{1,2})日?"
        ),
        "end_date": re.compile(
            r"(?:(?P<year>\d{4})[-/年])?(?P<month>\d{1,2})(?:[-/月])(?P<day>\d{1,2})日?"
        ),
    }
    pattern = patterns.get(name)
    if pattern is None or (match := pattern.search(content)) is None:
        return None
    if name == "budget":
        number = _number(match.group("value"))
        factor = {"万": 10_000, "千": 1_000, "k": 1_000, "K": 1_000}.get(match.group("unit"), 1)
        value: Any = number * factor
    elif name in {"days", "travelers"}:
        value = int(_number(match.group("value")))
    else:
        try:
            value = date(
                int(match.group("year") or today.year),
                int(match.group("month")),
                int(match.group("day")),
            ).isoformat()
        except ValueError:
            return None
    return value, match.start(), match.end()


def _number(value: str) -> float:
    chinese = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    return float(chinese[value]) if value in chinese else float(value)


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空文本")
    return value.strip()


def _normalize_region(value: str) -> str:
    normalized = _REGION_ALIASES.get(value, value)
    if normalized not in _REGION_NAMES:
        raise ValueError(f"暂时无法确认“{value}”是省级地区")
    return normalized


_REGION_ALIASES = {
    "北京": "北京市",
    "天津": "天津市",
    "上海": "上海市",
    "重庆": "重庆市",
    "河北": "河北省",
    "山西": "山西省",
    "辽宁": "辽宁省",
    "吉林": "吉林省",
    "黑龙江": "黑龙江省",
    "江苏": "江苏省",
    "浙江": "浙江省",
    "安徽": "安徽省",
    "福建": "福建省",
    "江西": "江西省",
    "山东": "山东省",
    "河南": "河南省",
    "湖北": "湖北省",
    "湖南": "湖南省",
    "广东": "广东省",
    "海南": "海南省",
    "四川": "四川省",
    "贵州": "贵州省",
    "云南": "云南省",
    "陕西": "陕西省",
    "甘肃": "甘肃省",
    "青海": "青海省",
    "台湾": "台湾省",
    "内蒙古": "内蒙古自治区",
    "广西": "广西壮族自治区",
    "西藏": "西藏自治区",
    "宁夏": "宁夏回族自治区",
    "新疆": "新疆维吾尔自治区",
    "香港": "香港特别行政区",
    "澳门": "澳门特别行政区",
}
_REGION_NAMES = frozenset(_REGION_ALIASES.values())
