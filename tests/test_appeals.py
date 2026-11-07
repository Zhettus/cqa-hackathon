"""Этап 6: апелляции — статусы, журнал, пересчёт final через scoring (исходная оценка неизменна)."""
import asyncio
import sqlite3
from decimal import Decimal

import pytest

from backend import db
from backend.appeals import apply_final_totals, final_result
from backend.evaluator import Evaluator
from backend.scoring import score
from tests.test_evaluator import OK, ScriptedProvider, answer


@pytest.fixture
def evaluated(edge_records):
    """edge-ru-001 оценён подставным провайдером: c01 yes, c02 na, c03 na, c04 20, c05 yes, c06 7 (guard),
    c07 yes, c08 no → 93.08."""
    conn = db.connect(":memory:")
    db.init_db(conn)
    db.save_dataset(conn, "EDGE", "local", list(edge_records.values()))
    ev = asyncio.run(Evaluator(conn, ScriptedProvider({cid: [answer(cid)] for cid in OK})).evaluate("edge-ru-001"))
    assert ev["status"] == "completed" and ev["total"] == 93.08
    yield conn, ev
    conn.close()


def _expected(conn, ev, **subs) -> float:
    cl = db.get_checklist(conn, "chat-quality", 1)
    answers = {**{c["criterion_id"]: c["answer"] for c in ev["criteria"]}, **subs}
    return float(score(cl, answers).total)


def test_status_flow_and_journal(evaluated):
    conn, ev = evaluated
    ap = db.insert_appeal(conn, ev["id"], "c06", "ожидание было объявлено", "op")
    assert ap["status"] == "open" and [e["event"] for e in ap["events"]] == ["opened"]
    ap = db.start_review(conn, ap["id"], "qa")
    assert ap["status"] == "in_review" and ap["events"][-1] == {**ap["events"][-1], "event": "in_review", "actor": "qa"}
    with pytest.raises(db.Conflict):
        db.start_review(conn, ap["id"], "qa")                                 # уже на рассмотрении
    with pytest.raises(db.ValidationFailed):
        db.insert_decision(conn, ap["id"], "change", new_answer=10)           # причина обязательна
    ap = db.insert_decision(conn, ap["id"], "change", new_answer=10, reason="предупреждение в #10", reviewer="qa")
    assert ap["status"] == "changed" and [e["event"] for e in ap["events"]] == ["opened", "in_review", "decided"]
    assert "change" in ap["events"][-1]["note"] and ap["decision"]["reason"] == "предупреждение в #10"
    with pytest.raises(db.Conflict):
        db.start_review(conn, ap["id"])
    with pytest.raises(sqlite3.DatabaseError):                                # журнал append-only
        conn.execute("DELETE FROM appeal_events WHERE appeal_id = ?", (ap["id"],))
    # решение сразу из open: событие in_review записывается автоматически
    ap2 = db.insert_appeal(conn, ev["id"], "c05", "вежлив", "op")
    ap2 = db.insert_decision(conn, ap2["id"], "confirm", reviewer="qa")
    assert ap2["status"] == "confirmed" and [e["event"] for e in ap2["events"]] == ["opened", "in_review", "decided"]
    assert ap2["events"][1]["note"] == "автоматически при решении"
    # фильтры по статусам
    assert [a["id"] for a in db.query_appeals(conn, status="changed")] == [ap["id"]]
    assert [a["id"] for a in db.query_appeals(conn, status="confirmed")] == [ap2["id"]]
    assert len(db.query_appeals(conn, status="decided")) == 2 and db.query_appeals(conn, status="active") == []
    ap3 = db.insert_appeal(conn, ev["id"], "c07", "попрощался", "op")
    assert [a["id"] for a in db.query_appeals(conn, status="open")] == [ap3["id"]]
    db.start_review(conn, ap3["id"])
    assert [a["id"] for a in db.query_appeals(conn, status="in_review")] == [ap3["id"]]
    assert db.query_appeals(conn, status="open") == [] and len(db.query_appeals(conn, status="active")) == 1


def test_final_recomputed_original_untouched(evaluated):
    conn, ev = evaluated
    base = final_result(conn, ev)
    assert base["total"] == 93.08 and base["changed"] == 0
    assert all(c["source"] == "original" and c["appeal_id"] is None for c in base["criteria"])
    ap = db.insert_appeal(conn, ev["id"], "c06", "x", "op")
    db.insert_decision(conn, ap["id"], "change", new_answer=10, reason="r", reviewer="qa")
    fin = final_result(conn, db.get_evaluation(conn, ev["id"]))
    assert fin["total"] == 100.0 == _expected(conn, ev, c06=10) and fin["changed"] == 1
    c06 = next(c for c in fin["criteria"] if c["criterion_id"] == "c06")
    assert c06 == {**c06, "answer": 10, "points": 15.0, "applicable": True, "source": "appeal", "appeal_id": ap["id"]}
    conduct = next(s for s in fin["sections"] if s["id"] == "conduct")
    assert conduct["score"] == 100.0
    # исходная оценка не изменилась
    orig = db.get_evaluation(conn, ev["id"])
    assert orig["total"] == 93.08 and next(c for c in orig["criteria"] if c["criterion_id"] == "c06")["answer"] == 7
    # confirm ничего не меняет
    ap2 = db.insert_appeal(conn, ev["id"], "c04", "x", "op")
    db.insert_decision(conn, ap2["id"], "confirm", reviewer="qa")
    assert final_result(conn, db.get_evaluation(conn, ev["id"]))["total"] == 100.0
    # повторная апелляция по тому же критерию после решения: последнее решение побеждает
    ap3 = db.insert_appeal(conn, ev["id"], "c06", "ещё раз", "op")
    db.insert_decision(conn, ap3["id"], "change", new_answer=4, reason="r2", reviewer="qa")
    fin3 = final_result(conn, db.get_evaluation(conn, ev["id"]))
    assert fin3["total"] == _expected(conn, ev, c06=4) == 86.15
    assert next(c for c in fin3["criteria"] if c["criterion_id"] == "c06")["appeal_id"] == ap3["id"]


def test_final_with_na_criterion(evaluated):
    conn, ev = evaluated
    # N/A → ответ: критерий становится применимым, вес 15 входит в знаменатель
    ap = db.insert_appeal(conn, ev["id"], "c02", "уточнение требовалось", "op")
    db.insert_decision(conn, ap["id"], "change", new_answer="no", reason="не уточнил", reviewer="qa")
    fin = final_result(conn, db.get_evaluation(conn, ev["id"]))
    c02 = next(c for c in fin["criteria"] if c["criterion_id"] == "c02")
    assert c02["applicable"] and c02["points"] == 0.0 and c02["source"] == "appeal"
    assert fin["total"] == _expected(conn, ev, c02="no") == 75.63
    # ответ → N/A: критерий исключается из знаменателя
    ap2 = db.insert_appeal(conn, ev["id"], "c04", "запроса не было", "op")
    db.insert_decision(conn, ap2["id"], "change", new_answer="na", reason="чат оборвался", reviewer="qa")
    fin2 = final_result(conn, db.get_evaluation(conn, ev["id"]))
    c04 = next(c for c in fin2["criteria"] if c["criterion_id"] == "c04")
    assert not c04["applicable"] and c04["points"] is None and c04["answer"] == "na"
    assert fin2["total"] == _expected(conn, ev, c02="no", c04="na") == Decimal("67.50")
    # N/A там, где он не разрешён, отклоняется на уровне scoring
    from backend.scoring import AnswerError, validate_answer
    c01 = next(c for c in db.get_checklist(conn, "chat-quality", 1)["criteria"] if c["id"] == "c01")
    with pytest.raises(AnswerError):
        validate_answer(c01, "na")


def test_final_with_flag(evaluated):
    conn, ev = evaluated
    ap = db.insert_appeal(conn, ev["id"], "c08", "была просьба перевести лично", "qa_lead")
    db.insert_decision(conn, ap["id"], "change", new_answer="yes", reason="реплика #11", reviewer="qa")
    fin = final_result(conn, db.get_evaluation(conn, ev["id"]))
    assert fin["total"] == 93.08                                  # флаг баллов не даёт — итог не меняется
    assert fin["flags"] == [{"criterion_id": "c08", "title": "Грубые ошибки", "answer": "yes"}]
    c08 = next(c for c in fin["criteria"] if c["criterion_id"] == "c08")
    assert c08["source"] == "appeal" and c08["points"] is None and not c08["applicable"]
    assert db.get_evaluation(conn, ev["id"])["flags"] == [{"criterion_id": "c08", "title": "Грубые ошибки",
                                                           "answer": "no"}]


def test_apply_final_totals_for_lists(evaluated):
    conn, ev = evaluated
    summaries = list(db.latest_evaluations(conn).values())
    apply_final_totals(conn, summaries)
    assert summaries[0]["final_total"] == 93.08 and "final_answers" not in summaries[0]
    ap = db.insert_appeal(conn, ev["id"], "c06", "x", "op")
    db.insert_decision(conn, ap["id"], "change", new_answer=10, reason="r")
    summaries = list(db.latest_evaluations(conn).values())
    apply_final_totals(conn, summaries)
    assert summaries[0]["final_total"] == 100.0 and summaries[0]["final_answers"]["c06"] == 10
    assert summaries[0]["total"] == 93.08


def test_final_none_for_not_evaluable(edge_records):
    conn = db.connect(":memory:")
    db.init_db(conn)
    db.save_dataset(conn, "EDGE", "local", list(edge_records.values()))
    ev = asyncio.run(Evaluator(conn, ScriptedProvider({})).evaluate("edge-ru-005"))
    assert final_result(conn, ev) is None
