"""Абстракция провайдера LLM. Провайдер и модель — из .env (LLM_PROVIDER, LLM_MODEL, ...).

Переключение провайдера — одной строкой `LLM_PROVIDER=anthropic | gemini | ollama | groq | openrouter | openai | vllm`:
у каждого вида есть профиль со значениями по умолчанию (модель, адрес, переменная с ключом, лимит запросов,
таймаут, параллельность). Любое из них переопределяется обычной переменной .env (LLM_MODEL, LLM_BASE_URL,
LLM_API_KEY, RATE_LIMIT_RPM, LLM_TIMEOUT_S, LLM_CONCURRENCY). Пустая переменная = не задана.
"""
from __future__ import annotations

import os

from backend import config  # noqa: F401  (загружает .env)
from backend.llm.base import PROVIDER_ERROR_CODES, LLMError, LLMProvider, Prices, Usage, retry_rate_limited

__all__ = ["LLMError", "LLMProvider", "PROVIDER_ERROR_CODES", "PROFILES", "Prices", "Usage", "DEFAULT_MODELS",
           "get_provider", "retry_rate_limited"]

# key_env — переменные с ключом по порядку предпочтения; key — фиксированное значение (локальные серверы).
PROFILES: dict[str, dict] = {
    "anthropic": {"model": "claude-opus-5-5", "key_env": ("LLM_API_KEY", "ANTHROPIC_API_KEY")},
    "gemini": {"model": "gemini-3.8-flash", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "key_env": ("GEMINI_API_KEY", "GEMINI_API", "GOOGLE_API_KEY", "LLM_API_KEY"),
               # бесплатный тариф: 20 запросов в сутки на модель (сброс в полночь по тихоокеанскому времени),
               # ответ 53–239 с на диалог в режиме per_dialog → длинный таймаут
               "rpm": 5, "timeout_s": 600, "max_retries": 3, "concurrency": 3},
    "ollama": {"model": "cqa-qwen14b", "base_url": "http://localhost:11434/v1", "key": "ollama",
               "concurrency": 1, "timeout_s": 900, "max_retries": 1},
    "vllm": {"base_url": "http://localhost:8000/v1", "key": "vllm", "concurrency": 1, "timeout_s": 900},
    "groq": {"model": "llama-3.3-70b-versatile", "base_url": "https://api.groq.com/openai/v1",
             "key_env": ("GROQ_API_KEY", "LLM_API_KEY"), "rpm": 30},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "key_env": ("OPENROUTER_API_KEY", "LLM_API_KEY"), "rpm": 20},
    "openai": {"key_env": ("LLM_API_KEY", "OPENAI_API_KEY")},      # любой OpenAI-совместимый адрес: LLM_BASE_URL обязателен
}
DEFAULT_MODELS = {k: v["model"] for k, v in PROFILES.items() if "model" in v}


def _env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    return v if v not in (None, "") else default


def _key(profile: dict) -> str | None:
    if "key" in profile:
        return profile["key"]
    return next((v for n in profile.get("key_env", ()) if (v := _env(n))), None)


def get_provider() -> LLMProvider:
    kind = (_env("LLM_PROVIDER", "anthropic") or "").lower()
    profile = PROFILES.get(kind)
    if profile is None:
        raise ValueError(f"неизвестный LLM_PROVIDER={kind!r} ({' | '.join(PROFILES)})")
    model = _env("LLM_MODEL", profile.get("model"))
    if not model:
        raise ValueError(f"LLM_MODEL не задан (для LLM_PROVIDER={kind} модели по умолчанию нет)")
    common = dict(timeout_s=float(_env("LLM_TIMEOUT_S", str(profile.get("timeout_s", 120)))),
                  max_retries=int(_env("LLM_MAX_RETRIES", str(profile.get("max_retries", 3)))),
                  concurrency=int(_env("LLM_CONCURRENCY", str(profile.get("concurrency", 8)))),
                  rate_limit_rpm=int(_env("RATE_LIMIT_RPM", str(profile.get("rpm", 0)))),
                  prices=Prices.from_env(model))
    api_key = _key(profile)
    if kind == "anthropic":
        from backend.llm.anthropic_provider import AnthropicProvider
        return AnthropicProvider(model, api_key=api_key, base_url=_env("LLM_BASE_URL"),
                                 effort=_env("LLM_EFFORT", "high"),
                                 max_tokens=int(_env("LLM_MAX_TOKENS", "16000")),
                                 fallbacks=_env("LLM_FALLBACKS", "1") not in ("0", "false", "no"), **common)
    from backend.llm.openai_compat import OpenAICompatProvider
    base_url = _env("LLM_BASE_URL", profile.get("base_url"))
    if not base_url:
        raise ValueError(f"для LLM_PROVIDER={kind} нужен LLM_BASE_URL (например http://localhost:11434/v1)")
    temp = _env("LLM_TEMPERATURE", "0")
    provider = OpenAICompatProvider(model, base_url=base_url, api_key=api_key,
                                    max_tokens=int(_env("LLM_MAX_TOKENS", "8000")),
                                    temperature=None if temp == "none" else float(temp), **common)
    provider.name = kind          # в llm_calls.provider — вид провайдера (gemini/ollama/…), а не «openai»
    return provider
