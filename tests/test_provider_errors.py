"""Ошибки провайдера (auth / billing / rate_limit): не failed, пакет останавливается или ждёт; лимит RPM."""
import asyncio
import json
import time

import httpx
import pytest

from backend import db
from backend.evaluator import Evaluator
from backend.llm import PROVIDER_ERROR_CODES, LLMError, retry_rate_limited
from backend.llm.anthropic_provider import is_billing_message
from backend.llm.base import LLMProvider, Prices, Usage
from backend.llm.openai_compat import OpenAICompatProvider
from tests.test_evaluator import OK, ScriptedProvider, answer


def _conn(edge_records):
    conn = db.connect(":memory:")
    db.init_db(conn)
    db.save_dataset(conn, "EDGE", "local", list(edge_records.values()))
    return conn


@pytest.mark.parametrize("code", sorted(PROVIDER_ERROR_CODES))
def test_provider_error_is_raised_not_failed(edge_records, code):
    conn = _conn(edge_records)
    script = {cid: [answer(cid)] for cid in OK}
    script["c03"] = [LLMError("провайдер недоступен", code=code)]
    with pytest.raises(LLMError) as e:
        asyncio.run(Evaluator(conn, ScriptedProvider(script)).evaluate("edge-ru-001"))
    assert e.value.code == code
    assert conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 0          # оценка не сохранена
    logged = conn.execute("SELECT status, json_extract(validation_errors_json, '$[0].code') FROM llm_calls "
                          "WHERE status = 'provider_error'").fetchall()
    assert logged and logged[0][1] == code                                               # но вызов залогирован


def test_generic_provider_error_still_failed(edge_records):
    conn = _conn(edge_records)
    script = {cid: [answer(cid)] for cid in OK}
    script["c03"] = [LLMError("HTTP 503")]
    ev = asyncio.run(Evaluator(conn, ScriptedProvider(script)).evaluate("edge-ru-001"))
    assert ev["status"] == "failed" and "503" in ev["status_reason"]


def test_retry_rate_limited_backoff():
    calls = {"n": 0}
    waits = []

    async def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise LLMError("429", code="rate_limit")
        return "ok"

    async def fake_sleep(s):
        waits.append(s)

    out = asyncio.run(retry_rate_limited(fn, attempts=5, base_s=2, sleep=fake_sleep))
    assert out == "ok" and calls["n"] == 3 and waits == [2, 4]                            # 2 × 2^n

    async def always():
        raise LLMError("429", code="rate_limit")
    with pytest.raises(LLMError) as e:
        asyncio.run(retry_rate_limited(always, attempts=2, base_s=1, sleep=fake_sleep))
    assert e.value.code == "rate_limit" and waits[-2:] == [1, 2]

    async def auth():
        raise LLMError("401", code="auth")
    with pytest.raises(LLMError) as e:
        asyncio.run(retry_rate_limited(auth, attempts=3, base_s=1, sleep=fake_sleep))
    assert e.value.code == "auth" and len(waits) == 4                                     # без ожидания


class _Fast(LLMProvider):
    name = "fast"

    def __init__(self, **kw):
        super().__init__("m", prices=Prices(), **kw)
        self.starts = []

    async def _call(self, system, user, schema, cache_prefix):
        self.starts.append(time.monotonic())
        return '{"a": 1}', Usage(provider=self.name, model=self.model)


def test_rate_limit_rpm_spaces_requests():
    p = _Fast(rate_limit_rpm=600, concurrency=8)                       # не чаще 1 запроса в 0.1 с

    async def go():
        await asyncio.gather(*(p.complete_json("s", "u", {}) for _ in range(4)))
    t0 = time.monotonic(); asyncio.run(go()); elapsed = time.monotonic() - t0
    gaps = [b - a for a, b in zip(sorted(p.starts), sorted(p.starts)[1:])]
    assert elapsed >= 0.28 and all(g >= 0.09 for g in gaps)
    q = _Fast(rate_limit_rpm=0, concurrency=8)
    t0 = time.monotonic(); asyncio.run(asyncio.gather(*(q.complete_json("s", "u", {}) for _ in range(4)))) if False else None
    async def go2():
        await asyncio.gather(*(q.complete_json("s", "u", {}) for _ in range(4)))
    t0 = time.monotonic(); asyncio.run(go2()); assert time.monotonic() - t0 < 0.1


def test_openai_compat_billing_and_rate_limit(monkeypatch):
    async def _noop(*_):
        return None
    monkeypatch.setattr(asyncio, "sleep", lambda *_: _noop())

    def prov(handler):
        return OpenAICompatProvider("m", base_url="http://llm.test/v1", transport=httpx.MockTransport(handler),
                                    prices=Prices(), max_retries=2)
    with pytest.raises(LLMError) as e:
        asyncio.run(prov(lambda r: httpx.Response(402, text="Insufficient credits")).complete_json("s", "u", {}))
    assert e.value.code == "billing"
    with pytest.raises(LLMError) as e:
        asyncio.run(prov(lambda r: httpx.Response(400, json={"error": {"message": "quota exceeded for this billing period"}}))
                    .complete_json("s", "u", {}))
    assert e.value.code == "billing"
    n = {"c": 0}

    def limited(r):
        n["c"] += 1
        return httpx.Response(429, text="rate limit exceeded")
    with pytest.raises(LLMError) as e:
        asyncio.run(prov(limited).complete_json("s", "u", {}))
    assert e.value.code == "rate_limit" and n["c"] == 3                                  # 1 + 2 повтора внутри провайдера
    with pytest.raises(LLMError) as e:
        asyncio.run(prov(lambda r: httpx.Response(401)).complete_json("s", "u", {}))
    assert e.value.code == "auth"


def test_anthropic_billing_message():
    assert is_billing_message("Your credit balance is too low to access the Anthropic API.")
    assert is_billing_message("Insufficient credits; please check billing")
    assert not is_billing_message("model: claude-x is not a valid model")
    assert not is_billing_message(None)


# --- пакет: остановка при биллинге, ожидание при 429 -------------------------------------------
def _app(tmp_path, provider):
    import os
    os.environ.setdefault("DB_PATH", str(tmp_path / "unused.db"))
    from fastapi.testclient import TestClient
    from backend.main import create_app
    from tests.conftest import ROOT
    app = create_app(db_path=tmp_path / "b.db", data_dir=ROOT / "data", provider=provider)
    return TestClient(app)


def _wait(client, bid, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        b = client.get(f"/api/batches/{bid}").json()
        if b["status"] in ("completed", "cancelled", "failed"):
            return b
        time.sleep(0.05)
    raise AssertionError(b)


class _BillingAfterOne(ScriptedProvider):
    """Первый диалог оценивается, затем провайдер отвечает ошибкой биллинга."""
    def __init__(self, script):
        super().__init__(script)
        self.dialogs = set()

    async def _call(self, system, user, schema, cache_prefix):
        self.dialogs.add(cache_prefix[:40])
        if len(self.dialogs) > 1:
            raise LLMError("Your credit balance is too low", code="billing")
        return await super()._call(system, user, schema, cache_prefix)


def test_batch_stops_on_billing(tmp_path):
    prov = _BillingAfterOne({cid: [answer(cid, quote="", checked_range={"from": 1, "to": 1})] for cid in OK})
    with _app(tmp_path, prov) as client:
        client.post("/api/datasets/import", json={"group_id": "EDGE", "prefer": "local"})
        r = client.post("/api/batches", json={"conversation_ids": ["edge-ru-001", "edge-ru-002", "edge-ru-003",
                                                                   "edge-ru-004"], "concurrency": 1})
        b = _wait(client, r.json()["batch_id"])
        assert b["status"] == "failed" and "биллинг" in b["error"] and "сохранены" in b["error"]
        statuses = {i["conversation_id"]: i["status"] for i in b["items"]}
        assert statuses["edge-ru-001"] == "completed"
        assert statuses["edge-ru-002"] == "provider_error" and statuses["edge-ru-004"] == "provider_error"
        assert b["progress"]["provider_error"] == 3 and b["progress"]["failed"] == 0
        evs = client.get("/api/evaluations", params={"batch_id": b["batch_id"]}).json()
        assert evs["total"] == 1                                                         # для provider_error записей нет
        r = client.post("/api/evaluations", json={"conversation_id": "edge-ru-002"})
        assert r.status_code == 502 and r.json()["error"]["code"] == "llm_billing"


class _RateLimitedOnce(ScriptedProvider):
    def __init__(self, script):
        super().__init__(script)
        self.limited = set()

    async def _call(self, system, user, schema, cache_prefix):
        key = cache_prefix[:40]
        if key not in self.limited:
            self.limited.add(key)
            raise LLMError("HTTP 429", code="rate_limit")
        return await super()._call(system, user, schema, cache_prefix)


def test_batch_waits_on_rate_limit(tmp_path, monkeypatch):
    import backend.llm.base as base
    monkeypatch.setattr(base, "RATE_LIMIT_BACKOFF_S", 0.01)
    prov = _RateLimitedOnce({cid: [answer(cid, quote="", checked_range={"from": 1, "to": 1})] for cid in OK})
    with _app(tmp_path, prov) as client:
        client.post("/api/datasets/import", json={"group_id": "EDGE", "prefer": "local"})
        r = client.post("/api/batches", json={"conversation_ids": ["edge-ru-001", "edge-ru-002"], "concurrency": 2})
        b = _wait(client, r.json()["batch_id"])
        assert b["status"] == "completed" and all(i["status"] == "completed" for i in b["items"])
        assert len(prov.limited) == 2                                                    # по одному 429 на диалог
        calls = client.get("/api/llm-calls", params={"batch_id": b["batch_id"], "status": "provider_error"}).json()
        assert calls["total"] == 2 and calls["items"][0]["validation_errors"][0]["code"] == "rate_limit"
