"""Assistant 对话模型的唯一构造入口。"""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from runtime.config import Settings


def create_chat_model(settings: Settings) -> BaseChatModel | None:
    """根据运行配置创建模型；未配置密钥时返回空值并由调用方明确报错。"""

    if settings.model_api_key is None:
        return None
    return ChatOpenAI(
        api_key=SecretStr(settings.model_api_key),
        base_url=settings.model_base_url,
        model=settings.fast_model,
        temperature=0,
        timeout=settings.model_timeout_seconds,
        max_retries=0,
    )
