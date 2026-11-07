"""Абстракция провайдера LLM. Провайдер и модель — из .env (LLM_PROVIDER, LLM_MODEL, ...)."""
from __future__ import annotations

import os

from backend import config  # noqa: F401  (загружает .env)
from backend.llm.base import PROVIDER_ERROR_CODES, LLMError, LLMProvider, Prices, Usage, retry_rate_limited

__all__ = ["LLMError", "LLMProvider", "PROVIDER_ERROR_CODES", "Prices", "Usage", "get_provider", "retry_rate_limited"]

DEFAULT_MODELS = {"anthropic": "claude-opus-5-5"}


def _env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    return v if v not in (None, "") else default


def get_provider() -> LLMProvider:
    kind = (_env("LLM_PROVIDER", "anthropic") or "").lower()
    model = _env("LLM_MODEL", DEFAULT_MODELS.get(kind))
    if not model:
        raise ValueError("LLM_MODEL не задан")
    common = dict(timeout_s=float(_env("LLM_TIMEOUT_S", "120")),
                  max_retries=int(_env("LLM_MAX_RETRIES", "3")),
                  concurrency=int(_env("LLM_CONCURRENCY", "8")),
                  rate_limit_rpm=int(_env("RATE_LIMIT_RPM", "0")),
                  prices=Prices.from_env(model))
    if kind == "anthropic":
        from backend.llm.anthropic_provider import AnthropicProvider
        return AnthropicProvider(model, api_key=_env("LLM_API_KEY"), base_url=_env("LLM_BASE_URL"),
                                 effort=_env("LLM_EFFORT", "high"),
                                 max_tokens=int(_env("LLM_MAX_TOKENS", "16000")),
                                 fallbacks=_env("LLM_FALLBACKS", "1") not in ("0", "false", "no"), **common)
    if kind in ("openai", "ollama", "vllm", "gemini", "groq", "openrouter"):
        from backend.llm.openai_compat import OpenAICompatProvider
        temp = _env("LLM_TEMPERATURE", "0")
        return OpenAICompatProvider(model, base_url=_env("LLM_BASE_URL", ""), api_key=_env("LLM_API_KEY"),
                                    max_tokens=int(_env("LLM_MAX_TOKENS", "8000")),
                                    temperature=None if temp == "none" else float(temp), **common)
    raise ValueError(f"неизвестный LLM_PROVIDER={kind!r} (anthropic | openai | ollama | gemini | groq | openrouter)")
