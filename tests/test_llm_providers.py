"""Провайдеры на подставных транспортах (без сети)."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from backend.llm.base import LLMError, Prices
from backend.llm.openai_compat import OpenAICompatProvider

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"],
          "additionalProperties": False}


def ok_body(content, usage=None):
    return {"id": "r1", "model": "qwen", "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": usage or {"prompt_tokens": 120, "completion_tokens": 30}}


def provider(handler, **kw):
    return OpenAICompatProvider("qwen", base_url="http://llm.test/v1", transport=httpx.MockTransport(handler),
                                prices=Prices(input=1.0, output=2.0), max_retries=2, **kw)


def test_openai_json_schema_and_usage():
    seen = []

    def h(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=ok_body('{"answer": "yes"}'))

    data, u = asyncio.run(provider(h).complete_json("sys", "user", SCHEMA, cache_prefix="dialog"))
    assert data == {"answer": "yes"} and u.tokens_in == 120 and u.tokens_out == 30
    assert u.cost_usd == pytest.approx((120 * 1 + 30 * 2) / 1e6) and u.raw_response == '{"answer": "yes"}'
    body = seen[0]
    assert body["response_format"]["type"] == "json_schema" and body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["messages"][1]["content"] == "dialog\n\nuser"


def test_openai_falls_back_to_json_object():
    formats = []

    def h(req):
        b = json.loads(req.content)
        formats.append(b["response_format"]["type"])
        if b["response_format"]["type"] == "json_schema":
            return httpx.Response(400, text="unsupported response_format json_schema")
        return httpx.Response(200, json=ok_body('{"answer": "no"}'))

    data, _ = asyncio.run(provider(h).complete_json("s", "u", SCHEMA))
    assert data == {"answer": "no"} and formats == ["json_schema", "json_object"]


def test_openai_retries_5xx(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", lambda *_: _noop())
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        return httpx.Response(503, text="busy") if n["c"] < 3 else httpx.Response(200, json=ok_body('{"answer":"x"}'))

    data, _ = asyncio.run(provider(h).complete_json("s", "u", SCHEMA))
    assert data == {"answer": "x"} and n["c"] == 3


async def _noop():
    return None


def test_openai_errors(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", lambda *_: _noop())
    with pytest.raises(LLMError) as e:
        asyncio.run(provider(lambda r: httpx.Response(401)).complete_json("s", "u", SCHEMA))
    assert e.value.code == "auth"
    with pytest.raises(LLMError) as e:
        asyncio.run(provider(lambda r: httpx.Response(200, json=ok_body("не json"))).complete_json("s", "u", SCHEMA))
    assert e.value.code == "bad_json" and e.value.usage.raw_response == "не json"
    with pytest.raises(LLMError):
        asyncio.run(provider(lambda r: httpx.Response(503)).complete_json("s", "u", SCHEMA))


def test_openai_requires_base_url():
    with pytest.raises(ValueError):
        OpenAICompatProvider("m", base_url="")


def test_anthropic_request_shape_and_refusal():
    from backend.llm.anthropic_provider import FALLBACK_BETA, AnthropicProvider
    p = AnthropicProvider("claude-opus-5-5", api_key="test", effort="high")
    sent = {}

    def response(stop="end_turn", text='{"answer": "yes"}'):
        return SimpleNamespace(
            model="claude-opus-5-5", stop_reason=stop, stop_details=SimpleNamespace(category="cyber"),
            content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=50, output_tokens=10, cache_read_input_tokens=1000,
                                  cache_creation_input_tokens=0), _request_id="req_1")

    async def create(**kw):
        sent.update(kw)
        return sent.pop("_resp", None) or response(stop=kw.pop("_stop", "end_turn"))

    p.client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    data, u = asyncio.run(p.complete_json("SYS", "USER", SCHEMA, cache_prefix="DIALOG"))
    assert data == {"answer": "yes"}
    assert sent["betas"] == [FALLBACK_BETA] and sent["fallbacks"] == "default"
    assert sent["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}, "effort": "high"}
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
    blocks = sent["messages"][0]["content"]
    assert blocks[0] == {"type": "text", "text": "DIALOG", "cache_control": {"type": "ephemeral"}}
    assert blocks[1] == {"type": "text", "text": "USER"}
    # цена Opus 5.5 по умолчанию: $4 вход, $20 выход, $0.20 чтение кэша за 1M
    assert u.cache_read_tokens == 1000 and u.cost_usd == pytest.approx((50 * 4 + 10 * 20 + 1000 * 0.2) / 1e6)

    async def refuse(**kw):
        return response(stop="refusal", text="")
    p.client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=refuse)))
    with pytest.raises(LLMError) as e:
        asyncio.run(p.complete_json("S", "U", SCHEMA))
    assert e.value.code == "refusal"
