"""Configuration helpers for BRT3."""

from __future__ import annotations

import os
from dataclasses import dataclass


DEFAULT_TOP_CODE = 6
DEFAULT_TOP_TESTS = 5
DEFAULT_MAX_WORKERS = 6
DEFAULT_MAX_FEEDBACK_ROUNDS = 3
DEFAULT_TIMEOUT = 120
DEFAULT_TEMPERATURE = 0.1
DEFAULT_MAX_TOKENS = 4096
DEFAULT_MODEL = "deepseek-v3"
DEFAULT_LLM_REQUEST_TIMEOUT = 300
DEFAULT_LLM_MAX_ATTEMPTS = 6
DEFAULT_LLM_BACKOFF_BASE = 15.0
DEFAULT_LLM_RATE_LIMIT_BACKOFF = 30.0


@dataclass
class LLMConfig:
    model: str = DEFAULT_MODEL
    api_key: str | None = None
    base_url: str | None = None
    temperature: float = DEFAULT_TEMPERATURE
    max_tokens: int = DEFAULT_MAX_TOKENS


def get_env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in ("", None) else default


def load_llm_config(
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> LLMConfig:
    """Load OpenAI-compatible DeepSeek settings without exposing secrets."""

    resolved_key = (
        api_key
        or get_env("DEEPSEEK_API_KEY")
        or get_env("OPENAI_API_KEY")
        or get_env("CSU_API_KEY")
        or get_env("API_KEY")
    )
    resolved_base = (
        base_url
        or get_env("DEEPSEEK_BASE_URL")
        or get_env("OPENAI_BASE_URL")
        or get_env("OPENAI_API_BASE")
        or get_env("CSU_BASE_URL")
        or "https://api.deepseek.com"
    )
    resolved_model = model or get_env("DEEPSEEK_MODEL") or DEFAULT_MODEL
    return LLMConfig(
        model=resolved_model,
        api_key=resolved_key,
        base_url=resolved_base.rstrip("/") if resolved_base else None,
        temperature=temperature,
        max_tokens=max_tokens,
    )
