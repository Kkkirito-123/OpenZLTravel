"""LangChain 对话 Agent 的创建与执行边界。"""

from __future__ import annotations

from typing import Any, cast

from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel

from assistant.errors import AssistantModelError
from assistant.fact_service import AssistantFactService
from assistant.language import build_system_prompt
from assistant.model_output import last_message_text
from assistant.models import AssistantSnapshot
from assistant.tools import build_langchain_tools


class ConversationRunner:
    """使用只读事实工具生成回复，不直接签发工单或读取 Graph 状态。"""

    def __init__(self, model: BaseChatModel | None) -> None:
        self.model = model

    async def respond(
        self,
        snapshot: AssistantSnapshot,
        facts: AssistantFactService,
    ) -> str:
        """运行一次 Agent 工具循环并返回最终自然语言消息。"""

        if self.model is None:
            raise AssistantModelError("旅行助手未配置 LLM，已拒绝切换到规则问答。")
        agent = create_agent(
            self.model,
            tools=build_langchain_tools(facts),
            system_prompt=build_system_prompt(snapshot),
        )
        messages = [
            {"role": item.role, "content": item.content}
            for item in snapshot.messages[-20:]
        ]
        try:
            result = await agent.ainvoke(cast(Any, {"messages": messages}))
            content = last_message_text(result).strip()
            if not content:
                raise AssistantModelError("LLM 未返回对话回复。")
            return content
        except AssistantModelError:
            raise
        except Exception as error:
            raise AssistantModelError("LLM 对话调用失败，请稍后重试。") from error
