"""OpenAI-совместимый /v1/chat/completions (Ollama, vLLM, LM Studio и т.п.) через httpx."""
from __future__ import annotations

import asyncio

import httpx

from backend.llm.base import LLMError, LLMProvider, Usage

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}


class OpenAICompatProvider(LLMProvider):
    name = "openai"

    def __init__(self, model: str, *, base_url: str, api_key: str | None = None, max_tokens: int = 8000,
                 temperature: float | None = 0.0, transport: httpx.AsyncBaseTransport | None = None, **kw):
        super().__init__(model, **kw)
        if not base_url:
            raise ValueError("для LLM_PROVIDER=openai нужен LLM_BASE_URL, например http://localhost:11434/v1")
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        self.temperature = temperature
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.client = httpx.AsyncClient(timeout=self.timeout_s, headers=headers, transport=transport)
        self._json_schema_supported = True

    def request_params(self) -> dict:
        return {"base_url": self.base_url, "max_tokens": self.max_tokens, "temperature": self.temperature}

    def _body(self, system, user, schema, cache_prefix):
        body = {
            "model": self.model, "max_tokens": self.max_tokens,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": f"{cache_prefix}\n\n{user}" if cache_prefix else user}],
        }
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self._json_schema_supported:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "evaluation", "schema": schema, "strict": True}}
        else:
            body["response_format"] = {"type": "json_object"}
        return body

    async def _call(self, system, user, schema, cache_prefix):
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                r = await self.client.post(f"{self.base_url}/chat/completions",
                                           json=self._body(system, user, schema, cache_prefix))
            except httpx.HTTPError as e:
                last = LLMError(f"нет соединения с {self.base_url}: {e}")
            else:
                if r.status_code == 400 and self._json_schema_supported and "response_format" in r.text:
                    self._json_schema_supported = False      # сервер не знает json_schema → json_object
                    continue
                if r.status_code == 429:
                    last = LLMError(f"HTTP 429: лимит запросов провайдера: {r.text[:300]}", code="rate_limit")
                elif r.status_code in RETRY_STATUS:
                    last = LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
                elif r.status_code in (401, 403):
                    raise LLMError(f"HTTP {r.status_code}: доступ запрещён, проверьте LLM_API_KEY", code="auth")
                elif r.status_code == 402 or (r.status_code == 400 and any(
                        m in r.text.lower() for m in ("insufficient", "credit", "billing", "quota"))):
                    raise LLMError(f"HTTP {r.status_code}: баланс/квота провайдера исчерпаны: {r.text[:300]}",
                                   code="billing")
                elif r.status_code >= 400:
                    raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
                else:
                    data = r.json()
                    choice = data["choices"][0]
                    u = data.get("usage") or {}
                    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
                    usage = Usage(provider=self.name, model=data.get("model", self.model),
                                  tokens_in=(u.get("prompt_tokens") or 0) - cached,
                                  tokens_out=u.get("completion_tokens") or 0, cache_read_tokens=cached,
                                  request_id=data.get("id"), stop_reason=choice.get("finish_reason"))
                    text = choice["message"].get("content") or ""
                    usage.raw_response = text
                    if choice.get("finish_reason") == "length":
                        raise LLMError("ответ обрезан по max_tokens", usage, code="max_tokens")
                    return text, usage
            if attempt < self.max_retries:
                await asyncio.sleep(min(2 ** attempt, 20))
        raise last or LLMError("неизвестная ошибка")
