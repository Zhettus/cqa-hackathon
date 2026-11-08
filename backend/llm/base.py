"""Интерфейс провайдера LLM: complete_json(system, user, schema) → (dict, Usage)."""
from __future__ import annotations

import asyncio
import json
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

# Цены USD за 1M токенов для известных моделей; переопределяются LLM_PRICE_* в .env.
KNOWN_PRICES: dict[str, dict[str, float]] = {
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.20},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.20},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50},
    "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_read": 0.25},
    # Gemini API, платный тариф (aistudio.google.com/docs/pricing, цены до 2026-12-31); на бесплатном тарифе $0
    "gemini-3.8-flash": {"input": 0.75, "output": 3.75},
    "gemini-3.6-flash": {"input": 0.75, "output": 3.75},
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50},
}


@dataclass
class Prices:
    input: float = 0.0          # USD / 1M токенов
    output: float = 0.0
    cache_read: float | None = None
    cache_write: float | None = None

    @classmethod
    def from_env(cls, model: str) -> "Prices":
        base = KNOWN_PRICES.get(model, {})

        def env(name: str, default):
            v = os.getenv(name)
            return float(v) if v not in (None, "") else default

        inp = env("LLM_PRICE_INPUT_PER_MTOK", base.get("input", 0.0))
        return cls(
            input=inp,
            output=env("LLM_PRICE_OUTPUT_PER_MTOK", base.get("output", 0.0)),
            cache_read=env("LLM_PRICE_CACHE_READ_PER_MTOK", base.get("cache_read", inp * 0.1)),
            cache_write=env("LLM_PRICE_CACHE_WRITE_PER_MTOK", inp * 1.25),
        )

    def cost(self, u: "Usage") -> float:
        c = u.tokens_in * self.input + u.tokens_out * self.output
        c += u.cache_read_tokens * (self.cache_read if self.cache_read is not None else self.input)
        c += u.cache_write_tokens * (self.cache_write if self.cache_write is not None else self.input)
        return round(c / 1_000_000, 6)


@dataclass
class Usage:
    provider: str
    model: str
    tokens_in: int = 0               # некэшированные входные токены
    tokens_out: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    raw_response: str = ""
    request_id: str | None = None
    stop_reason: str | None = None
    extra: dict = field(default_factory=dict)


# Ошибки провайдера, при которых диалог НЕ помечается failed: оценка не сохраняется, решение принимает
# вызывающий (пакет/eval.run): auth и billing — остановить, rate_limit — подождать и повторить.
PROVIDER_ERROR_CODES = frozenset({"auth", "billing", "rate_limit"})
RATE_LIMIT_BACKOFF_S = float(os.getenv("RATE_LIMIT_BACKOFF_S", "15"))
RATE_LIMIT_ATTEMPTS = int(os.getenv("RATE_LIMIT_ATTEMPTS", "5"))


class LLMError(RuntimeError):
    """Сбой вызова (сеть, провайдер, отказ модели, невалидный JSON). usage — если ответ был."""

    def __init__(self, message: str, usage: Usage | None = None, code: str = "provider_error"):
        super().__init__(message)
        self.usage = usage
        # provider_error | auth | billing | rate_limit | bad_json | refusal | max_tokens | invalid_response
        self.code = code


async def retry_rate_limited(fn, *, attempts: int | None = None, base_s: float | None = None,
                             on_wait=None, sleep=asyncio.sleep):
    """Вызывает fn() (корутина); при LLMError(code=rate_limit) ждёт base_s × 2^n и повторяет, до attempts раз.
    Остальные ошибки пробрасываются сразу."""
    attempts = RATE_LIMIT_ATTEMPTS if attempts is None else attempts
    base_s = RATE_LIMIT_BACKOFF_S if base_s is None else base_s
    for n in range(attempts + 1):
        try:
            return await fn()
        except LLMError as e:
            if e.code != "rate_limit" or n >= attempts:
                raise
            wait = base_s * (2 ** n)
            if on_wait:
                on_wait(wait, n + 1)
            await sleep(wait)


class LLMProvider(ABC):
    name: str = "base"

    def __init__(self, model: str, *, timeout_s: float = 120.0, max_retries: int = 3,
                 concurrency: int = 8, prices: Prices | None = None, rate_limit_rpm: int = 0):
        self.model = model
        self.effort: str | None = None      # глубина рассуждения (есть не у всех провайдеров); часть ключа кэша оценок
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.prices = prices or Prices.from_env(model)
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        # RATE_LIMIT_RPM: не чаще N запросов в минуту (бесплатные тарифы); 0 — без ограничения
        self.rate_limit_rpm = max(0, int(rate_limit_rpm))
        self._min_interval = 60.0 / self.rate_limit_rpm if self.rate_limit_rpm else 0.0
        self._next_slot = 0.0
        self._slot_lock: asyncio.Lock | None = None

    async def _throttle(self) -> None:
        """Равномерно распределяет старты запросов: не чаще rate_limit_rpm в минуту."""
        if not self._min_interval:
            return
        if self._slot_lock is None:
            self._slot_lock = asyncio.Lock()
        async with self._slot_lock:
            now = time.monotonic()
            start = max(now, self._next_slot)
            self._next_slot = start + self._min_interval
        if start > now:
            await asyncio.sleep(start - now)

    async def complete_json(self, system: str, user: str, schema: dict, *,
                            cache_prefix: str | None = None) -> tuple[dict, Usage]:
        """system — стабильная часть; cache_prefix — общий для всех критериев диалога блок
        (кэшируется, если провайдер умеет); user — переменная часть. Ограничение параллельности — семафор."""
        async with self._semaphore:
            await self._throttle()
            t0 = time.perf_counter()
            try:
                text, usage = await self._call(system, user, schema, cache_prefix)
            except LLMError as e:
                if e.usage is not None:          # ответ был, но непригоден — тоже стоит денег
                    e.usage.latency_ms = int((time.perf_counter() - t0) * 1000)
                    e.usage.cost_usd = self.prices.cost(e.usage)
                raise
            usage.latency_ms = int((time.perf_counter() - t0) * 1000)
            usage.cost_usd = self.prices.cost(usage)
            usage.raw_response = text
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError) as e:
            raise LLMError(f"ответ не JSON: {e}", usage, code="bad_json") from e
        if not isinstance(data, dict):
            raise LLMError("ответ не JSON-объект", usage, code="bad_json")
        return data, usage

    def describe_request(self, system: str, user: str, schema: dict, cache_prefix: str | None) -> dict:
        """Что логируется в llm_calls.request_json."""
        return {"provider": self.name, "model": self.model, "system": system,
                "cache_prefix": cache_prefix, "user": user, "schema": schema, **self.request_params()}

    def request_params(self) -> dict:
        return {}

    @abstractmethod
    async def _call(self, system: str, user: str, schema: dict, cache_prefix: str | None) -> tuple[str, Usage]:
        """Возвращает сырой текст ответа и usage без стоимости/латентности."""
