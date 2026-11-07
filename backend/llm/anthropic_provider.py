"""Claude через официальный SDK: structured outputs (output_config.format), кэш промпта,
серверный fallback при отказе модели."""
from __future__ import annotations

import os

import anthropic

from backend.llm.base import LLMError, LLMProvider, Usage

FALLBACK_BETA = "server-side-fallback-2026-07-01"
_BILLING_MARKERS = ("credit balance", "billing", "insufficient credits", "quota", "баланс")


def is_billing_message(text: str | None) -> bool:
    t = (text or "").lower()
    return any(m in t for m in _BILLING_MARKERS)


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, model: str, *, api_key: str | None = None, base_url: str | None = None,
                 effort: str | None = "high", max_tokens: int = 16000, fallbacks: bool = True, **kw):
        super().__init__(model, **kw)
        self.effort = effort
        self.max_tokens = max_tokens
        self.fallbacks = fallbacks
        # Ключ: LLM_API_KEY → ANTHROPIC_API_KEY → профиль `ant auth login` (резолвит SDK).
        self.client = anthropic.AsyncAnthropic(
            api_key=api_key or os.getenv("ANTHROPIC_API_KEY") or None,
            base_url=base_url or None, timeout=self.timeout_s, max_retries=self.max_retries)

    def request_params(self) -> dict:
        return {"effort": self.effort, "max_tokens": self.max_tokens, "fallbacks": self.fallbacks}

    async def _call(self, system, user, schema, cache_prefix):
        content = []
        if cache_prefix:
            content.append({"type": "text", "text": cache_prefix, "cache_control": {"type": "ephemeral"}})
        content.append({"type": "text", "text": user})
        params = dict(
            model=self.model, max_tokens=self.max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": content}],
            output_config={"format": {"type": "json_schema", "schema": schema},
                           **({"effort": self.effort} if self.effort else {})},
        )
        try:
            if self.fallbacks:
                resp = await self.client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **params)
            else:
                resp = await self.client.messages.create(**params)
        except anthropic.BadRequestError as e:
            if is_billing_message(e.message):
                raise LLMError(f"баланс Anthropic исчерпан: {e.message}", code="billing") from e
            raise LLMError(f"400: {e.message}") from e
        except anthropic.AuthenticationError as e:
            raise LLMError("ключ Anthropic не принят (проверьте LLM_API_KEY / ANTHROPIC_API_KEY)", code="auth") from e
        except anthropic.PermissionDeniedError as e:
            raise LLMError(f"доступ запрещён: {e.message}", code="auth") from e
        except anthropic.RateLimitError as e:
            raise LLMError(f"лимит запросов: {e.message}", code="rate_limit") from e
        except anthropic.APIStatusError as e:
            if e.status_code == 402 or is_billing_message(getattr(e, "message", "")):
                raise LLMError(f"баланс Anthropic исчерпан: {e.message}", code="billing") from e
            if e.status_code in (429, 529):
                raise LLMError(f"HTTP {e.status_code}: {e.message}", code="rate_limit") from e
            raise LLMError(f"HTTP {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"нет соединения с API: {e}") from e
        except TypeError as e:          # SDK не нашёл ни ключа, ни токена, ни профиля
            if "authentication" in str(e).lower():
                raise LLMError("нет учётных данных Anthropic: задайте LLM_API_KEY или ANTHROPIC_API_KEY в .env "
                               "(или выполните `ant auth login`)", code="auth") from e
            raise

        u = resp.usage
        usage = Usage(provider=self.name, model=getattr(resp, "model", self.model),
                      tokens_in=u.input_tokens or 0, tokens_out=u.output_tokens or 0,
                      cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                      cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
                      request_id=getattr(resp, "_request_id", None), stop_reason=resp.stop_reason)
        text = next((b.text for b in resp.content if b.type == "text"), "")
        usage.raw_response = text
        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            raise LLMError(f"модель отказалась отвечать ({getattr(details, 'category', None)})", usage, code="refusal")
        if resp.stop_reason == "max_tokens":
            raise LLMError("ответ обрезан по max_tokens", usage, code="max_tokens")
        return text, usage
