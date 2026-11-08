"""Смягчение валидатора: разрешённый N/A с пустым na_reason и непустым reasoning принимается, причина берётся из
reasoning, ставится флаг na_reason_derived (решение пользователя 2026-11-08). Пустые оба — по-прежнему отклоняется."""
import asyncio

from backend import db
from backend.evaluator import Evaluator
from backend.preprocess import preprocess
from backend.validator import validate_response
from tests.test_evaluator import OK, ScriptedProvider, answer


def _resp(**kw):
    r = {"evidence": [], "checked_range": {"from": 10, "to": 12}, "na_reason": None,
         "reasoning": "Данных для ответа хватало с первой реплики клиента.", "answer": "na"}
    r.update(kw)
    return r


def test_na_reason_taken_from_reasoning(edge_records, checklist_v1):
    pre = preprocess(edge_records["edge-ru-001"], checklist_v1.get("settings") or {})
    c02 = next(c for c in checklist_v1["criteria"] if c["id"] == "c02")
    c01 = next(c for c in checklist_v1["criteria"] if c["id"] == "c01")
    r = _resp()
    assert validate_response(c02, r, pre) == []
    assert r["na_reason"] == "Данных для ответа хватало с первой реплики клиента." and r["na_reason_derived"] is True
    r = _resp(na_reason="")
    assert validate_response(c02, r, pre) == [] and r["na_reason_derived"] is True
    # оба пустые — отклоняется, как раньше
    r = _resp(na_reason=None, reasoning="  ")
    assert [i.code for i in validate_response(c02, r, pre)] == ["missing_na_reason"]
    # явный na_reason не подменяется и флага нет
    r = _resp(na_reason="Условие выполнено: #1.")
    assert validate_response(c02, r, pre) == [] and "na_reason_derived" not in r and r["na_reason"] == "Условие выполнено: #1."
    # N/A там, где он не разрешён, смягчение не спасает
    assert [i.code for i in validate_response(c01, _resp(), pre)] == ["na_not_allowed"]


def test_evaluation_records_flag_and_condition(edge_records):
    conn = db.connect(":memory:")
    db.init_db(conn)
    db.save_dataset(conn, "EDGE", "local", list(edge_records.values()))
    script = {cid: [answer(cid)] for cid in OK}
    script["c02"] = [answer("c02", ans="na", na_reason=None, reasoning="Клиент сразу назвал устройство и задачу.")]
    ev = asyncio.run(Evaluator(conn, ScriptedProvider(script)).evaluate("edge-ru-001"))
    assert ev["status"] == "completed" and ev["total"] == 93.08
    crit = {c["criterion_id"]: c for c in ev["criteria"]}
    assert crit["c02"]["answer"] == "na" and crit["c02"]["na_reason_derived"] is True
    assert crit["c02"]["evidence"]["na_condition"] == "Клиент сразу назвал устройство и задачу."
    assert crit["c03"]["na_reason_derived"] is False and crit["c01"]["na_reason_derived"] is False
    assert db.get_evaluation(conn, ev["id"])["criteria"][1]["na_reason_derived"] is True
    assert ev["cost"]["rejected_calls"] == 0
