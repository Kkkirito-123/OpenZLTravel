"""旅行助手对话内核的兼容导出层。

具体职责位于 ``dialogue_commands``、``dialogue_context``、``dialogue_generator`` 和
``dialogue_flow``。保留本模块使应用代码、测试和既有扩展无需迁移导入路径。
"""

from app.dialogue_commands import (
    MEMORY_SLOT_NAMES,
    SLOT_NAMES,
    CancelFlowCommand,
    CityResolver,
    ClearSlotCommand,
    CommandEffects,
    CommandGeneration,
    ConfirmCommand,
    DialogueDecision,
    ForgetMemoryCommand,
    IntentCache,
    RememberSlotCommand,
    RouteToChatCommand,
    SetSlotCommand,
    SlotName,
    StartFlowCommand,
    TravelCommand,
    TravelCommandBatch,
)
from app.dialogue_context import TravelContextAssembler
from app.dialogue_flow import (
    apply_commands,
    apply_memory_defaults,
    decide_next,
    parse_fast_commands,
    validate_memory_commands,
)
from app.dialogue_generator import PromptCacheTransport, TravelCommandGenerator

__all__ = [
    "MEMORY_SLOT_NAMES",
    "SLOT_NAMES",
    "CancelFlowCommand",
    "CityResolver",
    "ClearSlotCommand",
    "CommandEffects",
    "CommandGeneration",
    "ConfirmCommand",
    "DialogueDecision",
    "ForgetMemoryCommand",
    "IntentCache",
    "PromptCacheTransport",
    "RememberSlotCommand",
    "RouteToChatCommand",
    "SetSlotCommand",
    "SlotName",
    "StartFlowCommand",
    "TravelCommand",
    "TravelCommandBatch",
    "TravelCommandGenerator",
    "TravelContextAssembler",
    "apply_commands",
    "apply_memory_defaults",
    "decide_next",
    "parse_fast_commands",
    "validate_memory_commands",
]
