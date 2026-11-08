"""Режим EVAL_MODE=per_dialog: один вызов на диалог, JSON по всем критериям; тот же валидатор, guards, повторы."""
import asyncio
import json
import re

import pytest

from backend import db
from backend.evaluator import Evaluator
from backend.llm.base import LLMError, Usage
from backend.prompt import build_dialog_criteria_block, prompt_version, response_schema, response_schema_all
from tests.test_evaluator import OK, ScriptedProvider, answer

_CRIT = re.compile(r"^КРИТЕРИЙ (\w+):", re.M)


class DialogProvider(ScriptedProvider):
    """Отвечает на запрос по всем критериям сразу: {cid: ответ}. overrides — сценарий по попыткам."""

    def __init__(self, overrides=None, bad_json_first=False):
        super().__init__({})
        self.overrides = {k: list(v) for k, v in (overrides or {}).items()}
        self.bad_json_first = bad_json_first
        self.requests = []

    async def _call(self, system, user, schema, cache_prefix):
        ids = _CRIT.findall(user)
        self.requests.append((ids, user, schema))
        if self.bad_json_first and len(self.requests) == 1:
            return "не json", Usage(provider=self.name, model=self.model, tokens_in=10, tokens_out=1)
        out = {}
        for cid in ids:
            seq = self.overrides.get(cid)
            out[cid] = (seq.pop(0) if seq and len(seq) > 1 else (seq[0] if seq else answer(cid)))
        return json.dumps(out, ensure_ascii=False), Usage(provider=self.name, model=self.model,
                                                             tokens_in=3000, tokens_out=800)


def _conn(edge_records):
    conn = db.connect(":memory:")
    db.init_db(conn)
    db.save_dataset(conn, "EDGE", "local", list(edge_records.values()))
    return conn


def test_schema_and_block_from_config(checklist_v1):
    crits = checklist_v1["criteria"]
    schema = response_schema_all(crits)
    assert list(schema["properties"]) == [c["id"] for c in crits] and schema["required"] == [c["id"] for c in crits]
    assert schema["properties"]["c04"] == response_schema(next(c for c in crits if c["id"] == "c04"))
    block = build_dialog_criteria_block(crits, {"c08": [{"idx": 10, "match": "мне на карту", "label": "л"}]})
    assert "ВСЕМ 8 КРИТЕРИЯМ" in block and block.count("КРИТЕРИЙ c0") == 8 and "#10: «мне на карту»" in block
    assert "Оцени оператора по этому критерию" not in block
    assert "checked_range" in block and "na_reason" in block and "будет отклонён" in block   # явные требования к форме
    new = crits + [{"id": "c99", "section": "conduct", "title": "Новый", "type": "binary", "weight": 1,
                    "na_allowed": False, "description": "x"}]
    assert "c99" in response_schema_all(new)["properties"] and "КРИТЕРИЙ c99:" in build_dialog_criteria_block(new)


def test_per_dialog_one_call_same_result(edge_records):
    conn = _conn(edge_records)
    prov = DialogProvider()
    ev = asyncio.run(Evaluator(conn, prov, mode="per_dialog").evaluate("edge-ru-001"))
    assert ev["status"] == "completed" and ev["total"] == 93.08                       # как в per_criterion
    assert ev["cost"]["calls"] == 1 and len(prov.requests) == 1 and ev["eval_mode"] == "per_dialog"
    assert ev["prompt_version"].endswith("|dialog") and ev["prompt_version"] == prompt_version(mode="per_dialog")
    crit = {c["criterion_id"]: c for c in ev["criteria"]}
    assert crit["c06"]["answer"] == 7 and crit["c06"]["model_answer"] == 10               # guard применён
    assert crit["c01"]["evidence"]["quotes"][0]["text"] == "Меня зовут Арман"              # цитата — подстрока оригинала
    assert crit["c02"]["evidence"]["kind"] == "na_condition"
    call = db.get_llm_call(conn, ev["llm_call_ids"][0])
    assert call["request"]["criterion_id"] == "*" and call["request"]["mode"] == "per_dialog"
    # кэш различает режимы
    ev2 = asyncio.run(Evaluator(conn, prov, mode="per_criterion").evaluate("edge-ru-001", use_cache=True))
    assert ev2["id"] != ev["id"]
    ev3 = asyncio.run(Evaluator(conn, prov, mode="per_dialog").evaluate("edge-ru-001", use_cache=True))
    assert ev3["id"] == ev["id"] and ev3["cached"]


def test_per_dialog_repair_only_invalid_criteria(edge_records):
    conn = _conn(edge_records)
    prov = DialogProvider(overrides={"c01": [answer("c01", ans="na"), answer("c01")],              # N/A не разрешён
                                     "c04": [answer("c04", quote="This is not in the text"), answer("c04")]})
    ev = asyncio.run(Evaluator(conn, prov, mode="per_dialog").evaluate("edge-ru-001"))
    assert ev["status"] == "completed" and ev["total"] == 93.08 and len(prov.requests) == 2
    fb = prov.requests[1][1]
    assert "ОТКЛОНЁН ВАЛИДАТОРОМ по критериям c01, c04" in fb and "не разрешён" in fb and "не найдена" in fb
    statuses = [db.get_llm_call(conn, i)["status"] for i in ev["llm_call_ids"]]
    assert statuses == ["invalid_response", "ok"] and ev["cost"]["rejected_calls"] == 1
    errs = db.get_llm_call(conn, ev["llm_call_ids"][0])["validation_errors"]
    assert {e["criterion_id"] for e in errs} == {"c01", "c04"}


def test_per_dialog_gives_up_and_names_criterion(edge_records):
    conn = _conn(edge_records)
    prov = DialogProvider(overrides={"c05": [answer("c05", ans="maybe")]})                 # всегда невалидно
    ev = asyncio.run(Evaluator(conn, prov, mode="per_dialog").evaluate("edge-ru-001"))
    assert ev["status"] == "failed" and "c05" in ev["status_reason"] and len(prov.requests) == 3


def test_per_dialog_bad_json_retry_and_missing_object(edge_records):
    conn = _conn(edge_records)
    prov = DialogProvider(bad_json_first=True)
    ev = asyncio.run(Evaluator(conn, prov, mode="per_dialog").evaluate("edge-ru-001"))
    assert ev["status"] == "completed" and len(prov.requests) == 2

    class Missing(DialogProvider):
        async def _call(self, system, user, schema, cache_prefix):
            text, u = await super()._call(system, user, schema, cache_prefix)
            d = json.loads(text); d.pop("c07", None)
            return json.dumps(d, ensure_ascii=False), u
    prov2 = Missing()
    ev2 = asyncio.run(Evaluator(conn, prov2, mode="per_dialog").evaluate("edge-ru-001"))
    assert ev2["status"] == "failed" and "c07" in ev2["status_reason"] and "нет объекта" in ev2["status_reason"]


def test_per_dialog_votes_and_provider_error(edge_records):
    conn = _conn(edge_records)
    prov = DialogProvider(overrides={"c04": [answer("c04", ans=20), answer("c04", ans=10), answer("c04", ans=15)]})
    ev = asyncio.run(Evaluator(conn, prov, mode="per_dialog").evaluate("edge-ru-001", votes=3))
    assert ev["status"] == "completed" and ev["cost"]["calls"] == 3
    c04 = next(c for c in ev["criteria"] if c["criterion_id"] == "c04")
    assert c04["answer"] == 15 and c04["confidence"] == pytest.approx(0.333, abs=1e-3)

    class Billing(DialogProvider):
        async def _call(self, *a, **k):
            raise LLMError("credit balance", code="billing")
    with pytest.raises(LLMError) as e:
        asyncio.run(Evaluator(conn, Billing(), mode="per_dialog").evaluate("edge-ru-002"))
    assert e.value.code == "billing"
    assert conn.execute("SELECT COUNT(*) FROM evaluations WHERE conversation_id='edge-ru-002'").fetchone()[0] == 0


def test_invalid_mode_rejected(edge_records):
    with pytest.raises(ValueError):
        Evaluator(_conn(edge_records), DialogProvider(), mode="per_sentence")
