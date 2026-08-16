"""旅行助手命令、槽位与状态类型。

本文件只定义受限命令和纯数据结构，不读取对话记录、不调用模型，也不访问旅行供应商。
这样命令校验、Flow 和模型调用可以独立演进。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol, get_args

from pydantic import BaseModel, ConfigDict, Field
from re_zlagent.harness.context import ContextManifest  # type: ignore[import-untyped]

from app.models import (
    AssistantFlow,
    AssistantTokenUsage,
    MemorySlotName,
    PlanningRequest,
    SlotMetadata,
    TravelDialogueSlots,
    TravelDialogueState,
)

SlotName = Literal[
    "origin",
    "destination_region",
    "destination_city",
    "start_date",
    "end_date",
    "days",
    "budget",
    "travelers",
    "preferences",
    "dietary_preferences",
    "distance_preference",
    "pace",
    "hotel_level",
    "transport_mode",
    "notes",
]
SLOT_NAMES = frozenset(get_args(SlotName))
MEMORY_SLOT_NAMES = frozenset(get_args(MemorySlotName))


class _StrictCommand(BaseModel):
    """拒绝模型输出中的未知字段，避免命令面被静默扩大。"""

    model_config = ConfigDict(extra="forbid")


class StartFlowCommand(_StrictCommand):
    """开始一个受支持的旅行流程。"""

    type: Literal["start_flow"]
    flow: AssistantFlow


class SetSlotCommand(_StrictCommand):
    """设置一个允许由用户提供的旅行槽位。"""

    type: Literal["set_slot"]
    name: SlotName
    value: Any
    evidence: str = Field(default="", max_length=200)


class ClearSlotCommand(_StrictCommand):
    """只在用户明确撤销信息时清空槽位。"""

    type: Literal["clear_slot"]
    name: SlotName


class ConfirmCommand(_StrictCommand):
    """确认当前流程。"""

    type: Literal["confirm"]


class CancelFlowCommand(_StrictCommand):
    """取消当前流程。"""

    type: Literal["cancel_flow"]


class RouteToChatCommand(_StrictCommand):
    """表示消息不属于当前支持的旅行流程。"""

    type: Literal["route_to_chat"]


class RememberSlotCommand(_StrictCommand):
    """在用户明确要求时保存一项稳定旅行偏好。"""

    type: Literal["remember_slot"]
    name: MemorySlotName
    value: Any
    evidence: str = Field(default="", max_length=200)


class ForgetMemoryCommand(_StrictCommand):
    """在用户明确要求时删除一项长期偏好。"""

    type: Literal["forget_memory"]
    name: MemorySlotName


TravelCommand = Annotated[
    StartFlowCommand
    | SetSlotCommand
    | ClearSlotCommand
    | ConfirmCommand
    | CancelFlowCommand
    | RouteToChatCommand
    | RememberSlotCommand
    | ForgetMemoryCommand,
    Field(discriminator="type"),
]


class TravelCommandBatch(BaseModel):
    """一次模型调用产生的有限命令序列。"""

    model_config = ConfigDict(extra="forbid")
    commands: list[TravelCommand] = Field(min_length=1, max_length=10)


class CityResolver(Protocol):
    """用于验证具体目的城市的最小目录接口。"""

    def resolve_city(self, destination: str) -> Any:
        """按用户输入返回规范城市，无法确认时抛出 LookupError。"""


class IntentCache(Protocol):
    """意图结果缓存的最小边界，当前由 SQLite provider_cache 实现。"""

    def get_cache(self, provider: str, key: str) -> Any | None:
        """读取未过期的缓存值。"""

    def set_cache(self, provider: str, key: str, value: Any, ttl_seconds: int) -> None:
        """保存带有效期的缓存值。"""


@dataclass(frozen=True, slots=True)
class CommandGeneration:
    """模型命令与本轮实际读取的上下文清单。"""

    batch: TravelCommandBatch
    manifest: ContextManifest
    metadata: dict[str, Any]
    usage: AssistantTokenUsage
    source: Literal["intent_cache", "llm"] = "llm"


@dataclass(frozen=True, slots=True)
class GeneratedCommands:
    """可在相同提示之间共享的模型结果，不携带某个会话的上下文引用。"""

    batch: TravelCommandBatch
    metadata: dict[str, Any]
    usage: AssistantTokenUsage


@dataclass(frozen=True, slots=True)
class CommandEffects:
    """命令执行后供对话策略使用的非持久信号。"""

    route_to_chat: bool = False
    confirmed: bool = False
    cancelled: bool = False
    validation_message: str | None = None
    memory_upserts: dict[MemorySlotName, str | list[str]] | None = None
    memory_deletes: frozenset[MemorySlotName] = frozenset()


@dataclass(slots=True)
class CommandState:
    """命令批次处理期间使用的临时状态，处理完成前不会写入数据库。"""

    revision: int
    slots: TravelDialogueSlots
    metadata: dict[str, SlotMetadata]
    active_flow: AssistantFlow | None
    status: str
    changed: set[str]
    route_to_chat: bool = False
    confirmed: bool = False
    cancelled: bool = False
    validation_message: str | None = None
    memory_upserts: dict[MemorySlotName, str | list[str]] | None = None
    memory_deletes: set[MemorySlotName] | None = None


@dataclass(frozen=True, slots=True)
class DialogueDecision:
    """确定性 Flow 对一轮消息给出的下一步。"""

    state: TravelDialogueState
    reply: str
    missing_slots: tuple[str, ...] = ()
    planning_request: PlanningRequest | None = None
