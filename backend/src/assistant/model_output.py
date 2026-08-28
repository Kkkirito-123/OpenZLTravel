"""模型消息读取与结构化决策解析。"""

from __future__ import annotations

import json
import re
from typing import Any

from assistant.errors import AssistantModelError
from assistant.models import AssistantDecision


def message_content_text(message: Any) -> str:
    """把 LangChain 消息的文本或文本块统一转换为字符串。"""

    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict)
        )
    raise AssistantModelError("LLM 最终消息格式无效。")


def last_message_text(result: dict[str, Any]) -> str:
    """读取 Agent 返回的最后一条消息。"""

    messages = result.get("messages")
    if not isinstance(messages, list) or not messages:
        raise AssistantModelError("LLM 未返回最终消息。")
    return message_content_text(messages[-1])


def parse_decision(content: str) -> AssistantDecision:
    """从模型文本中提取并校验唯一的 AssistantDecision JSON。"""

    normalized = content.strip()
    if normalized.startswith("```"):
        normalized = re.sub(r"^```(?:json)?\s*", "", normalized)
        normalized = re.sub(r"\s*```$", "", normalized)
    start = normalized.find("{")
    end = normalized.rfind("}")
    if start < 0 or end <= start:
        raise AssistantModelError("LLM 未返回 AssistantDecision JSON。")
    try:
        payload = json.loads(normalized[start : end + 1])
        return AssistantDecision.model_validate(payload)
    except (json.JSONDecodeError, ValueError) as error:
        raise AssistantModelError("LLM 返回的 AssistantDecision 无效。") from error
