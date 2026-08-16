"""旅行助手的专属上下文装配。

上下文只用于理解当前旅行需求。它不加载 POI、车票、酒店、地图或 Skill 正文，避免
外部事实和大体量内容干扰意图识别。
"""

from __future__ import annotations

import json
from xml.sax.saxutils import escape

from re_zlagent.harness.context import (  # type: ignore[import-untyped]
    ContextInput,
    ContextManifest,
    ContextManifestBuilder,
    ContextTrust,
)
from re_zlagent.harness.conversation import (  # type: ignore[import-untyped]
    ConversationContext,
)

from app.models import TravelDialogueState, TravelMemory
from app.skills import get_skill


class TravelContextAssembler:
    """为意图识别构造分层、可观测且受上下文窗口保护的专属上下文。"""

    def __init__(self, max_chars: int = 5_000) -> None:
        self._builder = ContextManifestBuilder(max_chars=max_chars)

    def build(
        self,
        state: TravelDialogueState,
        conversation: ConversationContext | None,
        memories: tuple[TravelMemory, ...] = (),
    ) -> ContextManifest:
        """按状态、待答字段、Skill 合约、记忆、近期轮次和摘要的顺序装配。"""

        snapshot = {
            "active_flow": state.active_flow,
            "status": state.status,
            "slots": state.slots.model_dump(mode="json", exclude_none=True),
            "pending_slots": state.pending_slots,
        }
        recent = _render_recent(conversation, max_rounds=6, max_chars=650)
        summary = conversation.render_summary() if conversation is not None else ""
        skill = get_skill(state.active_flow)
        return self._builder.build(
            (
                ContextInput(
                    id="travel_dialogue_state",
                    source="travel_dialogue_store",
                    trust=ContextTrust.RUNTIME_STATE,
                    content=json.dumps(snapshot, ensure_ascii=False),
                    max_chars=900,
                ),
                ContextInput(
                    id="pending_question",
                    source="travel_dialogue_store",
                    trust=ContextTrust.RUNTIME_STATE,
                    content=state.last_question or "",
                    max_chars=160,
                ),
                ContextInput(
                    id="active_skill_contract",
                    source="travel_skill_registry",
                    trust=ContextTrust.HOST,
                    content=skill.context_contract() if skill else "",
                    max_chars=320,
                ),
                ContextInput(
                    id="confirmed_travel_memories",
                    source="travel_memory_store",
                    trust=ContextTrust.RECALLED_BACKGROUND,
                    content=_render_memories(memories),
                    max_chars=480,
                ),
                ContextInput(
                    id="recent_conversation",
                    source="conversation_store",
                    trust=ContextTrust.UNTRUSTED,
                    content=recent,
                    max_chars=1_600,
                ),
                ContextInput(
                    id="conversation_summary",
                    source="conversation_compaction",
                    trust=ContextTrust.RECALLED_BACKGROUND,
                    content=summary,
                    max_chars=600,
                ),
            )
        )


def _render_recent(context: ConversationContext | None, *, max_rounds: int, max_chars: int) -> str:
    """以完整轮次为单位保留近期记录，避免截断半轮对话造成错误归因。"""

    if context is None:
        return ""
    selected: list[str] = []
    used = 0
    for turn in reversed(context.recent_turns[-max_rounds:]):
        block = (
            f'<turn sequence="{turn.sequence}"><user>{escape(turn.user_content)}</user>'
            f"<assistant>{escape(turn.assistant_content)}</assistant></turn>"
        )
        if used + len(block) > max_chars:
            continue
        selected.append(block)
        used += len(block)
    return "\n".join(reversed(selected))


def _render_memories(memories: tuple[TravelMemory, ...]) -> str:
    """只渲染稳定偏好及版本，不携带来源会话和时间戳。"""

    payload = [{"key": item.key, "value": item.value, "version": item.version} for item in memories]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if payload else ""
