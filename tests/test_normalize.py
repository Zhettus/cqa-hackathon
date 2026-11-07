"""Этап 5: нормализация текста звонков моделью — валидация, кэш, применение, встраивание в оценку (без сети)."""
import asyncio
import json
import re

import pytest

from backend import db
from backend.evaluator import Evaluator
from backend.llm.base import Usage
from backend.normalize import apply_normalization, normalize_call, normalizer_version, targets, validate
from backend.preprocess import ASR_UNCERTAIN, preprocess
from backend.prompt import prompt_version
from tests.test_evaluator import OK, ScriptedProvider, answer

_LINE = re.compile(r"^#(\d+) \[(клиент|оператор)\]: (.*)$")


class NormalizingProvider(ScriptedProvider):
    """Подставной провайдер: на запросы нормализации отвечает «text (clean)», остальное — по сценарию."""

    def __init__(self, script, uncertain=(), bad_first=False):
        super().__init__(script)
        self.uncertain = set(uncertain)
        self.bad_first = bad_first
        self.norm_calls = []

    async def _call(self, system, user, schema, cache_prefix):
        if user.startswith("НОРМАЛИЗАЦИЯ"):
            self.norm_calls.append((system, user, cache_prefix))
            items = []
            for line in user.splitlines():
                m = _LINE.match(line)
                if m:
                    idx, text = int(m.group(1)), m.group(3)
                    items.append({"idx": idx, "text_clean": text + " (clean)",      # маски «[…]» сохраняются
                                  "asr_uncertain": idx in self.uncertain})
            if self.bad_first and len(self.norm_calls) == 1:
                items = items[1:]                                   # первая попытка — без одной реплики
            return json.dumps({"items": items}, ensure_ascii=False), Usage(provider=self.name, model=self.model,
                                                                            tokens_in=500, tokens_out=200)
        self.calls.append(("dialog", cache_prefix))
        return await super()._call(system, user, schema, cache_prefix)


def _conn(records):
    conn = db.connect(":memory:")
    db.init_db(conn)
    db.save_dataset(conn, "DEV", "local", list(records.values()))
    return conn


def test_validate_rules(dev_records):
    pre = preprocess(dev_records["p4-ru-002"])
    items = targets(pre)
    assert items and all(pre.messages[i - 1].speaker in ("client", "operator") for i in items)
    good = {"items": [{"idx": i, "text_clean": t, "asr_uncertain": False} for i, t in items.items()]}
    assert validate(good, items) == []
    first = next(iter(items))
    missing = {"items": good["items"][1:]}
    assert any("нет реплик" in e for e in validate(missing, items))
    extra = {"items": good["items"] + [{"idx": 9999, "text_clean": "x", "asr_uncertain": False}]}
    assert any("лишние idx" in e for e in validate(extra, items))
    empty = {"items": [{**o, "text_clean": "  "} if o["idx"] == first else o for o in good["items"]]}
    assert any(f"#{first}: пустой" in e for e in validate(empty, items))
    masked_idx = next((i for i, t in items.items() if "[" in t), None)
    if masked_idx is not None:
        lost = {"items": [{**o, "text_clean": "текст без маски"} if o["idx"] == masked_idx else o for o in good["items"]]}
        assert any("маскированные" in e for e in validate(lost, items))
    assert validate({"items": "nope"}, items) == ["items должен быть списком"]


def test_apply_keeps_original(dev_records):
    rec = dev_records["p4-ru-002"]
    before = json.dumps(rec, ensure_ascii=False)
    pre = preprocess(rec)
    norm = {3: {"text_clean": "Чистый текст три", "asr_uncertain": True}, 6: {"text_clean": "Чистый шесть", "asr_uncertain": False}}
    out = apply_normalization(pre, norm)
    by = {m.idx: m for m in out.messages}
    assert by[3].clean_text == "Чистый текст три" and ASR_UNCERTAIN in by[3].flags and by[3].text == rec["transcript"][2]["text"]
    assert by[6].clean_text == "Чистый шесть" and ASR_UNCERTAIN not in by[6].flags
    assert by[4].clean_text == pre.messages[3].clean_text and by[4].flags == pre.messages[3].flags
    assert pre.messages[2].clean_text != "Чистый текст три" and ASR_UNCERTAIN not in pre.messages[2].flags  # копия
    assert json.dumps(rec, ensure_ascii=False) == before
    assert by[3].counts_for_operator == pre.messages[2].counts_for_operator


def test_normalize_call_cache_and_log(dev_records):
    conn = _conn(dev_records)
    pre = preprocess(dev_records["p4-ru-002"])
    prov = NormalizingProvider({}, uncertain={3})
    norm, cached = asyncio.run(normalize_call(conn, prov, pre, evaluation_id="ev_x"))
    assert not cached and len(prov.norm_calls) == 1
    assert set(norm) == set(targets(pre)) and norm[3]["asr_uncertain"] and norm[3]["text_clean"].endswith("(clean)")
    calls = conn.execute("SELECT status, evaluation_id, request_json FROM llm_calls").fetchall()
    assert len(calls) == 1 and calls[0][0] == "ok" and calls[0][1] == "ev_x"
    assert json.loads(calls[0][2])["purpose"] == "normalize"
    norm2, cached2 = asyncio.run(normalize_call(conn, prov, pre))
    assert cached2 and norm2 == norm and len(prov.norm_calls) == 1          # из кэша, модель не вызывалась
    assert db.latest_normalization(conn, "p4-ru-002", __import__("backend.normalize", fromlist=["text_hash"]).text_hash(pre))["version"] == normalizer_version()
    # невалидный первый ответ → повтор с текстом ошибки, обе попытки залогированы
    conn2 = _conn(dev_records)
    prov2 = NormalizingProvider({}, bad_first=True)
    norm3, _ = asyncio.run(normalize_call(conn2, prov2, pre))
    assert len(prov2.norm_calls) == 2 and "ОТКЛОНЁН" in prov2.norm_calls[1][1] and set(norm3) == set(targets(pre))
    statuses = [r[0] for r in conn2.execute("SELECT status FROM llm_calls ORDER BY rowid")]
    assert statuses == ["invalid_response", "ok"]


def test_evaluator_uses_normalization_for_calls_only(dev_records, edge_records):
    conn = _conn(dev_records)
    db.save_dataset(conn, "EDGE", "local", list(edge_records.values()))
    script = {cid: [answer(cid, quote="", checked_range={"from": 1, "to": 1})] for cid in OK}
    prov = NormalizingProvider(script, uncertain={3})
    ev = asyncio.run(Evaluator(conn, prov, normalize_calls=True).evaluate("p4-ru-002"))
    assert ev["status"] == "completed", ev["status_reason"]
    assert ev["prompt_version"] == prompt_version(normalized=True) and "+n1-" in ev["prompt_version"]
    assert len(prov.norm_calls) == 1
    dialog = next(cp for kind, cp in prov.calls if kind == "dialog")
    assert "clean:" in dialog and "ASR_неуверенно" in dialog and "(clean)" in dialog
    system = conn.execute("SELECT request_json FROM llm_calls WHERE evaluation_id = ? AND "
                          "json_extract(request_json, '$.purpose') IS NULL LIMIT 1", (ev["id"],)).fetchone()[0]
    assert "корректора распознавания" in json.loads(system)["system"]
    assert ev["cost"]["calls"] == 9                                    # 8 критериев + 1 нормализация
    # исходный текст в БД и в карточке не тронут
    assert db.get_conversation(conn, "p4-ru-002")["transcript"] == dev_records["p4-ru-002"]["transcript"]
    # чат: нормализация не вызывается, версия промпта без суффикса
    ev2 = asyncio.run(Evaluator(conn, prov, normalize_calls=True).evaluate("edge-ru-001"))
    assert ev2["status"] == "completed" and len(prov.norm_calls) == 1 and "+" not in ev2["prompt_version"]
    # кэш оценок различает режимы
    ev3 = asyncio.run(Evaluator(conn, prov, normalize_calls=False).evaluate("p4-ru-002", use_cache=True))
    assert ev3["id"] != ev["id"] and "+" not in ev3["prompt_version"]
    ev4 = asyncio.run(Evaluator(conn, prov, normalize_calls=True).evaluate("p4-ru-002", use_cache=True))
    assert ev4["id"] == ev["id"] and ev4["cached"]


def test_prompt_versions_differ():
    assert prompt_version() != prompt_version(normalized=True)
    assert prompt_version(normalized=True).startswith(prompt_version() + "+")
    assert prompt_version(normalized=True).endswith(normalizer_version())
