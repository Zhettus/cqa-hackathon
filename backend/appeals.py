"""Финальный итог с учётом решений по апелляциям (этап 6).

Исходная оценка неизменна (триггеры в БД), решения хранятся отдельно. final = scoring.score по ответам
исходной оценки, в которых ответы критериев с решением «change» заменены новыми (последнее решение
по критерию побеждает). Баллы, N/A, секции и флаги считает только backend/scoring.py (инвариант 2).
"""
from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from backend import db
from backend.scoring import score, to_jsonable


def final_result(conn: sqlite3.Connection, evaluation: dict, appeals: list[dict] | None = None,
                 checklist: dict | None = None) -> dict | None:
    """Итог с решениями: {total, sections, flags, criteria[{criterion_id, answer, points, applicable, source,
    appeal_id}], changed}. None, если оценка не completed (нечего пересчитывать)."""
    if evaluation.get("status") != "completed":
        return None
    appeals = db.list_appeals(conn, evaluation["id"]) if appeals is None else appeals
    checklist = checklist or db.get_checklist(conn, evaluation["checklist"]["id"], evaluation["checklist"]["version"])
    answers: dict[str, Any] = {c["criterion_id"]: c["answer"] for c in evaluation["criteria"]}
    source: dict[str, tuple[str, str | None]] = {cid: ("original", None) for cid in answers}
    changed = [a for a in appeals if a.get("status") == "changed"]
    for a in sorted(changed, key=lambda a: a["decision"]["decided_at"]):
        if a["criterion_id"] in answers:
            answers[a["criterion_id"]] = a["decision"]["new_answer"]
            source[a["criterion_id"]] = ("appeal", a["id"])
    res = to_jsonable(score(checklist, answers))
    for c in res["criteria"]:
        c["source"], c["appeal_id"] = source[c["criterion_id"]]
    res["changed"] = sum(1 for s in source.values() if s[0] == "appeal")
    return res


def apply_final_totals(conn: sqlite3.Connection, summaries: Iterable[dict]) -> None:
    """Проставляет final_total (и final_answers) в краткие объекты оценок (списки, результаты).
    Пересчёт делается только для оценок, у которых есть решения «change»; остальным final_total = total."""
    changes = db.change_decisions(conn)
    for s in summaries:
        s.setdefault("final_total", s.get("total"))
        if s.get("id") in changes and s.get("status") == "completed":
            ev = db.get_evaluation(conn, s["id"])
            final = final_result(conn, ev)
            if final:
                s["final_total"] = final["total"]
                s["final_answers"] = {c["criterion_id"]: c["answer"] for c in final["criteria"]}
