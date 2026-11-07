"""Метрики и проверка edge в eval/run.py, и кэш оценок (тестовый провайдер, без сети)."""
import asyncio

import pytest

from backend import db
from backend.checklist import load_default_checklist
from backend.evaluator import Evaluator
from eval.run import compare, edge_verdict, metrics, render, step_error
from tests.test_evaluator import OK, ScriptedProvider, answer

CL = load_default_checklist()
C = {c["id"]: c for c in CL["criteria"]}


def test_step_error():
    assert step_error(C["c04"], 20, 10) == 2 and step_error(C["c06"], 10, 4) == 2
    assert step_error(C["c04"], "na", 10) is None


def _ev(conn, edge_records, script):
    db.save_dataset(conn, "EDGE", "local", list(edge_records.values()))
    return asyncio.run(Evaluator(conn, ScriptedProvider(script)).evaluate("edge-ru-001"))


def test_compare_metrics_edge_and_report(edge_records):
    conn = db.connect(":memory:")
    db.init_db(conn)
    script = {cid: [answer(cid)] for cid in OK}
    script["c04"] = [answer("c04", ans=10)]
    ev = _ev(conn, edge_records, script)
    gold = {"conversation_id": "edge-ru-001", "target": ["c06"], "facts": {"first_response_s": 311},
            "answers": {"c01": "yes", "c02": "na", "c03": "na", "c04": 20, "c05": "yes", "c06": 7, "c07": "yes",
                        "c08": "no"}, "total": 93.08, "uncertain": ["c04"], "notes": "n"}
    res = compare(ev, gold, CL, edge_records["edge-ru-001"])
    bad = [r for r in res["criteria"] if not r["match"]]
    assert [(r["criterion_id"], r["step_error"], r["uncertain"]) for r in bad] == [("c04", 2, True)]
    c06 = next(r for r in res["criteria"] if r["criterion_id"] == "c06")
    assert c06["match"] and c06["guards"][0]["after"] == 7
    e = edge_verdict(res, gold, edge_records["edge-ru-001"], CL)
    assert e["passed"] and not e["all_criteria_match"]           # target c06 совпал, c04 — нет
    mt = metrics([res], CL)
    assert mt["n"] == 8 and mt["match"] == 7 and mt["criteria"]["c04"]["step_err"] == [2]
    assert "язык RU" in mt["groups"] and "канал agent chat" in mt["groups"]
    md = render({"started": "t", "gold": ["g"], "prompt_version": "v", "model": "m", "votes": 1, "usd": 0,
                 "seconds": 1}, [res], mt, [e], CL)
    assert "uncertain" in md and "| edge-ru-001 ⚠️uncertain |" in md and "## Случаи edge" in md


def test_edge_fails_on_target_and_status(edge_records):
    conn = db.connect(":memory:")
    db.init_db(conn)
    db.save_dataset(conn, "EDGE", "local", list(edge_records.values()))
    ev = asyncio.run(Evaluator(conn, ScriptedProvider({})).evaluate("edge-ru-005"))
    gold = {"conversation_id": "edge-ru-005", "target": ["system"], "answers": None, "total": None,
            "expected_behavior": "not_evaluable", "facts": {"operator_messages": 0}}
    res = compare(ev, gold, CL, edge_records["edge-ru-005"])
    assert edge_verdict(res, gold, edge_records["edge-ru-005"], CL)["passed"]
    res2 = {**res, "status": "completed"}
    assert not edge_verdict(res2, gold, edge_records["edge-ru-005"], CL)["passed"]


def test_cache_by_key(edge_records, monkeypatch):
    conn = db.connect(":memory:")
    db.init_db(conn)
    db.save_dataset(conn, "EDGE", "local", list(edge_records.values()))
    script = {cid: [answer(cid)] for cid in OK}
    prov = ScriptedProvider(script)
    ev1 = asyncio.run(Evaluator(conn, prov).evaluate("edge-ru-001", use_cache=True))
    ev2 = asyncio.run(Evaluator(conn, prov).evaluate("edge-ru-001", use_cache=True))
    assert ev2["id"] == ev1["id"] and ev2["cached"] and len(prov.calls) == 8
    asyncio.run(Evaluator(conn, prov).evaluate("edge-ru-001", use_cache=True, votes=2))      # другой ключ
    assert len(prov.calls) == 8 + 16
    monkeypatch.setattr("backend.evaluator.prompt_version", lambda normalized=False, mode="per_criterion": "другая-версия")
    ev4 = asyncio.run(Evaluator(conn, prov).evaluate("edge-ru-001", use_cache=True))
    assert ev4["id"] != ev1["id"]
    ev5 = asyncio.run(Evaluator(conn, prov).evaluate("edge-ru-001"))                          # без кэша — новая
    assert ev5["id"] not in (ev1["id"], ev4["id"])
    monkeypatch.undo()
    prov.effort = "low"                                                                          # другой effort — новая
    ev6 = asyncio.run(Evaluator(conn, prov).evaluate("edge-ru-001", use_cache=True))
    assert ev6["id"] not in (ev1["id"], ev4["id"], ev5["id"]) and ev6["effort"] == "low"
    assert asyncio.run(Evaluator(conn, prov).evaluate("edge-ru-001", use_cache=True))["id"] == ev6["id"]


def test_migration_adds_columns(tmp_path):
    path = tmp_path / "old.db"
    c = db.connect(path)
    c.executescript("CREATE TABLE evaluations (id TEXT PRIMARY KEY, conversation_id TEXT, checklist_id TEXT, "
                    "checklist_version INTEGER, status TEXT, status_reason TEXT, total REAL, sections_json TEXT, "
                    "flags_json TEXT, facts_json TEXT, model_provider TEXT, model_name TEXT, batch_id TEXT, "
                    "duration_ms INTEGER, created_at TEXT);")
    db.init_db(c)
    cols = {r[1] for r in c.execute("PRAGMA table_info(evaluations)")}
    assert {"prompt_version", "votes", "effort"} <= cols


def test_failed_evaluation_counts_as_mismatch_everywhere():
    """B1: нет оценки при наличии эталона → ошибка по всем критериям, диалог в знаменателе точности и MAE."""
    gold = {"conversation_id": "x", "answers": {"c01": "yes", "c02": "na", "c03": "na", "c04": 20, "c05": "yes",
                                               "c06": 10, "c07": "yes", "c08": "no"}, "total": 100.0, "notes": ""}
    rec = {"conversation_id": "x", "language": "RU", "channel": "agent chat", "transcript": []}
    ok = compare({"conversation_id": "ok", "status": "completed", "total": 100.0, "criteria": [
        {"criterion_id": cid, "answer": a, "explanation": "", "evidence": {}} for cid, a in gold["answers"].items()]},
        gold, CL, rec)
    for status in ("failed", "not_evaluable"):
        bad = compare({"conversation_id": "x", "status": status, "status_reason": "HTTP 503", "total": None,
                       "criteria": []}, gold, CL, rec)
        assert bad["status_mismatch"] and len(bad["criteria"]) == 8
        assert all(not r["match"] and r["system"] == status and r["status_mismatch"] for r in bad["criteria"])
        mt = metrics([ok, bad], CL)
        assert mt["n"] == 16 and mt["match"] == 8 and mt["status_mismatch"] == ["x"]
        assert mt["total_compared"] == 2 and mt["total_exact"] == 1 and mt["total_mae"] == 50.0   # |100 − 0| / 2
        assert mt["criteria"]["c04"]["step_err"] == [0] and mt["criteria"]["c04"]["na_mismatch"] == 0   # [0] — от ok
        md = render({"started": "t", "gold": ["g"], "prompt_version": "v", "model": "m", "votes": 1, "usd": 0,
                     "seconds": 1}, [ok, bad], mt, [], CL)
        assert "Нет оценки при наличии эталона: 1" in md and f"x — {status} (HTTP 503)" in md
    # эталон не ждёт ответов (not_evaluable) — строк нет, диалог не в знаменателе
    ne = compare({"conversation_id": "y", "status": "not_evaluable", "total": None, "criteria": []},
                 {"conversation_id": "y", "answers": None, "total": None, "expected_behavior": "not_evaluable"}, CL, rec)
    assert not ne["status_mismatch"] and ne["criteria"] == [] and metrics([ne], CL)["n"] == 0


def test_accept_alternative_answers():
    """Поле accept эталона: допустимая альтернатива считается совпадением, итог пересчитывается под неё."""
    gold = {"conversation_id": "x", "answers": {"c01": "yes", "c02": "yes", "c03": "na", "c04": 10, "c05": "yes",
                                               "c06": 10, "c07": "yes", "c08": "no"}, "total": 87.5,
            "uncertain": ["c04"], "accept": {"c04": [0, 10]}, "notes": ""}
    rec = {"conversation_id": "x", "language": "RU", "channel": "agent chat", "transcript": []}

    def ev(c04, total):
        answers = {**gold["answers"], "c04": c04}
        return {"conversation_id": "x", "status": "completed", "total": total, "criteria": [
            {"criterion_id": cid, "answer": a, "explanation": "", "evidence": {}} for cid, a in answers.items()]}

    r0 = compare(ev(0, 75.0), gold, CL, rec)
    c04 = next(c for c in r0["criteria"] if c["criterion_id"] == "c04")
    assert c04["match"] and c04["accepted"] and c04["step_error"] == 0
    assert r0["accepted"] == {"c04": 0} and r0["gold_total"] == 75.0 and r0["gold_total_listed"] == 87.5
    r10 = compare(ev(10, 87.5), gold, CL, rec)
    assert all(c["match"] for c in r10["criteria"]) and "accepted" not in r10 and r10["gold_total"] == 87.5
    r5 = compare(ev(5, 81.25), gold, CL, rec)
    assert not next(c for c in r5["criteria"] if c["criterion_id"] == "c04")["match"]
    mt = metrics([r0, r10, r5], CL)
    assert mt["match"] == 23 and mt["total_mae"] == pytest.approx(6.25 / 3)     # только r5 даёт ошибку итога
    md = render({"started": "t", "gold": ["g"], "prompt_version": "v", "model": "m", "votes": 1, "usd": 0,
                 "seconds": 1}, [r0], mt, [], CL)
    assert "x/c04=0" in md
