from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI
from langchain_google_genai import ChatGoogleGenerativeAI

from .config import RoleConfig, infer_provider


def create_chat_model(
    role: RoleConfig,
    timeout_s: int,
) -> BaseChatModel:
    provider_name = infer_provider(role.model, role.base_url, role.provider)
    openai_style_providers = {"openai", "openai_compatible", "qwen", "deepseek"}

    if provider_name in openai_style_providers:
        return ChatOpenAI(
            api_key=role.api_key,
            base_url=role.base_url,
            model=role.model,
            timeout=timeout_s,
            max_retries=1,
        )

    if provider_name in {"google", "gemini"}:
        try:
            return ChatGoogleGenerativeAI(
                google_api_key=role.api_key,
                model=role.model,
                timeout=timeout_s,
                max_retries=1,
            )
        except TypeError:
            return ChatGoogleGenerativeAI(
                google_api_key=role.api_key,
                model=role.model,
            )

    raise ValueError(f"Unsupported provider: {provider_name}")
