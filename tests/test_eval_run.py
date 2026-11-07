"""Метрики и проверка edge в eval/run.py, и кэш оценок (тестовый провайдер, без сети)."""
import asyncio

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
    monkeypatch.setattr("backend.evaluator.prompt_version", lambda: "другая-версия")
    ev4 = asyncio.run(Evaluator(conn, prov).evaluate("edge-ru-001", use_cache=True))
    assert ev4["id"] != ev1["id"]
    ev5 = asyncio.run(Evaluator(conn, prov).evaluate("edge-ru-001"))                          # без кэша — новая
    assert ev5["id"] not in (ev1["id"], ev4["id"])


def test_migration_adds_columns(tmp_path):
    path = tmp_path / "old.db"
    c = db.connect(path)
    c.executescript("CREATE TABLE evaluations (id TEXT PRIMARY KEY, conversation_id TEXT, checklist_id TEXT, "
                    "checklist_version INTEGER, status TEXT, status_reason TEXT, total REAL, sections_json TEXT, "
                    "flags_json TEXT, facts_json TEXT, model_provider TEXT, model_name TEXT, batch_id TEXT, "
                    "duration_ms INTEGER, created_at TEXT);")
    db.init_db(c)
    cols = {r[1] for r in c.execute("PRAGMA table_info(evaluations)")}
    assert {"prompt_version", "votes"} <= cols
