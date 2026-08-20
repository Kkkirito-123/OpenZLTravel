"""三个 Agent 共用的异步 OpenAI 结构化输出网关。

网关只负责模型协议适配和连接池复用。各 Agent 的提示、Token 上限和总截止时间仍由
``graph.agents`` 管理，因此这里不做第二次“修复调用”，也不缓存意图识别结果。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from openai import AsyncOpenAI
from pydantic import BaseModel

from runtime.config import Settings
from runtime.contracts import ModelMessage, StructuredModel


class OpenAIStructuredModel:
    """通过 Responses API 的原生 Pydantic 解析生成严格结构化结果。"""

    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        self._client = client
        self._model = model

    async def ainvoke(
        self,
        messages: Sequence[ModelMessage],
        *,
        response_model: type[BaseModel],
        max_tokens: int,
    ) -> BaseModel | dict[str, Any]:
        """执行一次结构化调用；空解析结果按失败处理并交由图节点降级。"""

        response = await self._client.responses.parse(
            model=self._model,
            input=cast(Any, list(messages)),
            text_format=response_model,
            max_output_tokens=max_tokens,
            store=False,
        )
        if response.output_parsed is None:
            raise RuntimeError("模型没有返回可解析的结构化结果")
        return response.output_parsed


@dataclass(frozen=True, slots=True)
class ModelBundle:
    """图依赖装配使用的快慢模型集合。"""

    requirement: StructuredModel | None
    planner: StructuredModel | None
    review: StructuredModel | None


def build_model_bundle(settings: Settings) -> ModelBundle:
    """存在 API Key 时共享一个异步连接池，否则启用图内确定性降级。"""

    if settings.model_api_key is None:
        return ModelBundle(None, None, None)
    client = AsyncOpenAI(
        api_key=settings.model_api_key,
        base_url=settings.model_base_url,
        timeout=30.0,
        max_retries=0,
    )
    fast = OpenAIStructuredModel(client, settings.fast_model)
    strong = OpenAIStructuredModel(client, settings.planner_model)
    return ModelBundle(requirement=fast, planner=strong, review=fast)
