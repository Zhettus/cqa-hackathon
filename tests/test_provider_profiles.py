"""Профили провайдеров: переключение одной строкой LLM_PROVIDER=…, значения по умолчанию и их переопределение из .env."""
import pytest

from backend.llm import PROFILES, get_provider

ENV_KEYS = ["LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY", "RATE_LIMIT_RPM", "LLM_TIMEOUT_S",
            "LLM_CONCURRENCY", "LLM_MAX_RETRIES", "GEMINI_API_KEY", "GEMINI_API", "GOOGLE_API_KEY", "GROQ_API_KEY",
            "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"]


@pytest.fixture
def env(monkeypatch):
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_gemini_profile_from_single_line(env):
    env.setenv("LLM_PROVIDER", "gemini")
    env.setenv("GEMINI_API", "g-key")
    env.setenv("LLM_API_KEY", "anthropic-key")                       # ключ другого провайдера не используется
    p = get_provider()
    assert p.name == "gemini" and p.model == PROFILES["gemini"]["model"]
    assert p.base_url == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert p.client.headers["authorization"] == "Bearer g-key"
    assert p.rate_limit_rpm == 5 and p.timeout_s == 600 and p.max_retries == 3
    assert p.prices.input > 0                                         # цена известной модели — для стоимости в логах


def test_env_overrides_profile(env):
    env.setenv("LLM_PROVIDER", "gemini")
    env.setenv("GEMINI_API_KEY", "k2")
    env.setenv("LLM_MODEL", "gemini-3.6-flash")
    env.setenv("RATE_LIMIT_RPM", "0")
    env.setenv("LLM_TIMEOUT_S", "120")
    p = get_provider()
    assert p.model == "gemini-3.6-flash" and p.rate_limit_rpm == 0 and p.timeout_s == 120
    assert p.client.headers["authorization"] == "Bearer k2"


def test_ollama_local_profile(env):
    env.setenv("LLM_PROVIDER", "ollama")
    p = get_provider()
    assert p.name == "ollama" and p.model == "cqa-qwen14b" and p.base_url == "http://localhost:11434/v1"
    assert p.client.headers["authorization"] == "Bearer ollama" and p.timeout_s == 900 and p.max_retries == 1


def test_generic_openai_requires_base_url_and_unknown_kind(env):
    env.setenv("LLM_PROVIDER", "openai")
    env.setenv("LLM_MODEL", "x")
    with pytest.raises(ValueError, match="LLM_BASE_URL"):
        get_provider()
    env.setenv("LLM_PROVIDER", "openrouter")
    env.delenv("LLM_MODEL")                                           # у openrouter нет модели по умолчанию
    with pytest.raises(ValueError, match="LLM_MODEL"):
        get_provider()
    env.setenv("LLM_PROVIDER", "someone")
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        get_provider()


def test_anthropic_default(env):
    env.setenv("LLM_PROVIDER", "anthropic")
    env.setenv("LLM_API_KEY", "sk-test")
    p = get_provider()
    assert p.name == "anthropic" and p.model == "claude-opus-5-5"
