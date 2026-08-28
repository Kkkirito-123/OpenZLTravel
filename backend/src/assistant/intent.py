"""把自然语言输入提取为受约束的 AssistantDecision。"""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel

from assistant.errors import AssistantModelError
from assistant.language import build_decision_prompt
from assistant.model_output import message_content_text, parse_decision
from assistant.models import AssistantDecision, AssistantSnapshot


class IntentExtractor:
    """只负责结构化意图提取，不生成回复，也不调用旅行工具。"""

    def __init__(self, model: BaseChatModel | None) -> None:
        self.model = model

    async def decide(
        self,
        snapshot: AssistantSnapshot,
        user_text: str,
    ) -> AssistantDecision:
        """将本轮输入转换为严格决策模型。"""

        if self.model is None:
            raise AssistantModelError("旅行助手未配置 LLM，已拒绝切换到规则问答。")
        extractor = self.model.bind(response_format={"type": "json_object"})
        try:
            response = await extractor.ainvoke(build_decision_prompt(snapshot, user_text))
            return parse_decision(message_content_text(response))
        except AssistantModelError:
            raise
        except Exception as error:
            raise AssistantModelError("LLM 理解用户需求失败，请稍后重试。") from error
