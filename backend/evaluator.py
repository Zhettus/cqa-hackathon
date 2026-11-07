"""Оценка одного диалога: preprocess → вызовы LLM по критериям (параллельно) → валидация с повторами →
голосование → guards → scoring → сохранение. Каждый вызов модели логируется в llm_calls (инвариант 5)."""
from __future__ import annotations

import asyncio
import os
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from backend import db
from backend.checklist import effective_settings
from backend.guards import apply_guards
from backend.hints import find_hints
from backend.llm import LLMError, LLMProvider, get_provider
from backend.preprocess import Preprocessed, preprocess
from backend.prompt import build_criterion_block, build_dialog_block, build_system, prompt_version, response_schema
from backend.scoring import score, to_jsonable
from backend.validator import feedback_text, validate_response

MAX_REPAIRS = 2          # повторов после отказа валидатора (итого до 3 попыток на голос)
NOT_EVALUABLE_REASON = "Нет реплик оператора"


def default_votes() -> int:
    return max(1, int(os.getenv("VOTES", "1")))


@dataclass
class Vote:
    answer: Any
    resp: dict


@dataclass
class CriterionOutcome:
    criterion: dict
    votes: list[Vote] = field(default_factory=list)
    error: str | None = None


class Evaluator:
    def __init__(self, conn, provider: LLMProvider | None = None):
        self.conn = conn
        self.provider = provider or get_provider()

    # --- один голос по одному критерию с повторами ---------------------------------------
    async def _one_vote(self, ctx: dict, criterion: dict, vote_no: int) -> Vote:
        schema = response_schema(criterion)
        feedback = None
        last_error = "нет ответа"
        for attempt in range(1, MAX_REPAIRS + 2):
            user = build_criterion_block(criterion, ctx["hints"].get(criterion["id"]), feedback,
                                         others=[c for c in ctx["criteria"] if c["id"] != criterion["id"]])
            call = {"evaluation_id": ctx["evaluation_id"], "conversation_id": ctx["pre"].conversation_id,
                    "batch_id": ctx["batch_id"], "attempt": attempt, "provider": self.provider.name,
                    "model": self.provider.model, "started_at": db.now(),
                    "request": {**self.provider.describe_request(ctx["system"], user, schema, ctx["dialog"]),
                                "criterion_id": criterion["id"], "vote": vote_no}}
            try:
                resp, usage = await self.provider.complete_json(ctx["system"], user, schema,
                                                                cache_prefix=ctx["dialog"])
            except LLMError as e:
                u = e.usage
                self._log(call, u, status="invalid_response" if e.code == "bad_json" else "provider_error",
                          errors=[{"criterion_id": criterion["id"], "code": e.code, "message": str(e)}])
                last_error = str(e)
                if e.code != "bad_json" or u is None:
                    raise
                feedback = ("ПРЕДЫДУЩИЙ ОТВЕТ ОТКЛОНЁН: это не корректный JSON-объект по схеме. "
                            f"Предыдущий ответ: {u.raw_response[:2000]}\nВерни корректный JSON.")
                continue
            issues = validate_response(criterion, resp, ctx["pre"])
            self._log(call, usage, status="invalid_response" if issues else "ok",
                      errors=[{"criterion_id": criterion["id"], **i.as_dict()} for i in issues])
            if not issues:
                return Vote(answer=resp["answer"] if resp["answer"] != "na" else "na", resp=resp)
            last_error = "; ".join(i.message for i in issues)
            feedback = feedback_text(issues, usage.raw_response)
        raise LLMError(f"{criterion['id']}: ответ не прошёл проверку после {MAX_REPAIRS} повторов: {last_error}",
                       code="invalid_response")

    def _log(self, call: dict, usage, *, status: str, errors: list[dict]) -> None:
        if usage is not None:
            call.update(model=usage.model, latency_ms=usage.latency_ms, cost_usd=usage.cost_usd,
                        tokens_in=usage.tokens_in + usage.cache_read_tokens + usage.cache_write_tokens,
                        tokens_out=usage.tokens_out, raw_response=usage.raw_response)
            call["request"] = {**call["request"], "usage_detail": {
                "uncached_in": usage.tokens_in, "cache_read": usage.cache_read_tokens,
                "cache_write": usage.cache_write_tokens, "request_id": usage.request_id,
                "stop_reason": usage.stop_reason}}
        db.insert_llm_call(self.conn, {**call, "status": status, "validation_errors": errors})

    async def _criterion(self, ctx: dict, criterion: dict, n_votes: int) -> CriterionOutcome:
        out = CriterionOutcome(criterion)
        results = await asyncio.gather(*(self._one_vote(ctx, criterion, v) for v in range(1, n_votes + 1)),
                                       return_exceptions=True)
        for r in results:
            if isinstance(r, Vote):
                out.votes.append(r)
            elif isinstance(r, LLMError) and r.code == "auth":
                raise r                     # нет доступа к модели — оценивать бессмысленно
            elif isinstance(r, BaseException) and not isinstance(r, Exception):
                raise r
            else:
                out.error = str(r)
        if out.votes:
            out.error = None        # хотя бы один валидный голос — критерий оценён
        return out

    # --- полная оценка ---------------------------------------------------------------------
    async def evaluate(self, conversation_id: str, checklist_id: str = "chat-quality",
                       checklist_version: int | None = None, *, votes: int | None = None,
                       batch_id: str | None = None, use_cache: bool = False) -> dict:
        """use_cache=True — вернуть готовую оценку с тем же ключом (диалог, версия чек-листа, версия промпта,
        модель, голоса), если она есть. По умолчанию каждая оценка — новый вызов модели."""
        t0 = time.perf_counter()
        conv = db.get_conversation(self.conn, conversation_id)
        checklist = db.get_checklist(self.conn, checklist_id, checklist_version)
        n_votes = votes or default_votes()
        pv = prompt_version()
        if use_cache:
            cached = db.find_cached_evaluation(self.conn, conversation_id, checklist["id"], checklist["version"],
                                               pv, self.provider.model, n_votes)
            if cached:
                return {**cached, "cached": True}
        settings = effective_settings(checklist)
        eid = db.new_id("ev")
        base = {"id": eid, "conversation_id": conversation_id, "checklist_id": checklist["id"],
                "checklist_version": checklist["version"], "batch_id": batch_id, "facts": {},
                "model_provider": self.provider.name, "model_name": self.provider.model,
                "prompt_version": pv, "votes": n_votes}
        try:
            return await self._run(conv, checklist, settings, base, n_votes, t0)
        except LLMError as e:
            if e.code == "auth":                                   # нет доступа к модели — дальше бессмысленно
                raise
            reason = str(e)
        except Exception as e:                                     # любой сбой → failed, пакет продолжается
            reason = f"{type(e).__name__}: {e}"
        db.insert_evaluation(self.conn, {**base, "status": "failed", "total": None, "status_reason": reason,
                                         "duration_ms": int((time.perf_counter() - t0) * 1000)}, [])
        return db.get_evaluation(self.conn, eid)

    async def _run(self, conv: dict, checklist: dict, settings: dict, base: dict, n_votes: int, t0: float) -> dict:
        eid = base["id"]
        batch_id = base["batch_id"]
        pre = preprocess(conv, settings)
        base["facts"] = pre.facts.model_dump()

        if pre.facts.operator_messages == 0:                       # инвариант 10: LLM не вызывается
            db.insert_evaluation(self.conn, {**base, "status": "not_evaluable", "status_reason": NOT_EVALUABLE_REASON,
                                             "total": None, "model_provider": None, "model_name": None,
                                             "duration_ms": int((time.perf_counter() - t0) * 1000)}, [])
            return db.get_evaluation(self.conn, eid)

        ctx = {"evaluation_id": eid, "batch_id": batch_id, "pre": pre,
               "system": build_system(pre.channel), "dialog": build_dialog_block(pre, settings),
               "hints": {c["id"]: find_hints(c, pre) for c in checklist["criteria"]},
               "criteria": checklist["criteria"]}
        outcomes = await asyncio.gather(*(self._criterion(ctx, c, n_votes) for c in checklist["criteria"]))

        failed = [o for o in outcomes if not o.votes]
        if failed:
            db.insert_evaluation(self.conn, {**base, "status": "failed", "total": None,
                                             "status_reason": _failure_reason(failed),
                                             "duration_ms": int((time.perf_counter() - t0) * 1000)}, [])
            return db.get_evaluation(self.conn, eid)

        facts = pre.facts.model_dump()
        rows, answers = [], {}
        for o in outcomes:
            row = finalize_criterion(o.criterion, o.votes, facts, settings, ctx["hints"].get(o.criterion["id"], []))
            rows.append(row)
            answers[row["criterion_id"]] = row["answer"]
        result = to_jsonable(score(checklist, answers))
        pts = {c["criterion_id"]: c for c in result["criteria"]}
        for row in rows:
            row.update(points=pts[row["criterion_id"]]["points"], applicable=pts[row["criterion_id"]]["applicable"])
        db.insert_evaluation(self.conn, {**base, "status": "completed", "total": result["total"],
                                         "sections": result["sections"], "flags": result["flags"],
                                         "duration_ms": int((time.perf_counter() - t0) * 1000)}, rows)
        return db.get_evaluation(self.conn, eid)


# --- голосование и итог по критерию ----------------------------------------------------------
def aggregate_votes(criterion: dict, answers: list[Any]) -> tuple[Any, float]:
    """binary/flag — большинство (ничья → первый по порядку голос); scale — медиана ступеней
    (N/A побеждает только большинством). confidence — доля голосов, совпавших с итогом."""
    if criterion["type"] == "scale":
        na = sum(a == "na" for a in answers)
        nums = [a for a in answers if a != "na"]
        if na > len(nums) or not nums:
            final = "na"
        else:
            final = statistics.median_low(nums)
    else:
        counts = Counter(answers)
        top = max(counts.values())
        final = next(a for a in answers if counts[a] == top)
    return final, round(sum(a == final for a in answers) / len(answers), 3)


def finalize_criterion(criterion: dict, votes: list[Vote], facts: dict, settings: dict, hints: list[dict]) -> dict:
    final, confidence = aggregate_votes(criterion, [v.answer for v in votes])
    # обоснование и доказательства — из первого голоса с итоговым ответом (или ближайшего по шкале)
    rep = next((v for v in votes if v.answer == final), None)
    if rep is None:
        nums = [v for v in votes if v.answer != "na"]
        rep = min(nums, key=lambda v: abs(v.answer - final)) if nums else votes[0]
    answer, hits = apply_guards(criterion, final, facts, settings)
    r = rep.resp
    explanation = r.get("reasoning", "").strip()
    guard_notes = [h.explanation() for h in hits]
    if guard_notes:
        explanation = (explanation + " " + " ".join(guard_notes)).strip()
    if answer == "na":
        evidence = {"kind": "na_condition", "quotes": [], "range": None,
                    "na_condition": r.get("na_reason") or criterion.get("na_condition")}
    elif r.get("evidence"):
        evidence = {"kind": "quote", "quotes": [{"n": e["idx"], "text": e["quote"]} for e in r["evidence"]],
                    "range": _range(r), "na_condition": None}
    else:
        evidence = {"kind": "checked_range", "quotes": [], "range": _range(r), "na_condition": None}
    meta = {"confidence": confidence, "votes": [v.answer for v in votes], "model_answer": final,
            "guards": [{"id": h.guard_id, "conditions": h.conditions, "before": h.before, "after": h.after}
                       for h in hits], "hints": hints}
    if criterion["type"] == "flag":
        meta["violation_type"] = r.get("violation_type")
    return {"criterion_id": criterion["id"], "section": criterion["section"], "type": criterion["type"],
            "weight": criterion["weight"], "answer": answer, "explanation": explanation,
            "evidence": evidence, "meta": meta}


def _failure_reason(failed: list[CriterionOutcome]) -> str:
    """Одинаковые причины сбоя по разным критериям — одной строкой."""
    by_error: dict[str, list[str]] = {}
    for o in failed:
        by_error.setdefault(o.error or "ошибка", []).append(o.criterion["id"])
    return "; ".join(f"{', '.join(ids)}: {err}" for err, ids in by_error.items())


def _range(resp: dict) -> dict | None:
    rng = resp.get("checked_range")
    return None if not rng else {"from_n": rng["from"], "to_n": rng["to"]}


async def evaluate(conn, conversation_id: str, **kw) -> dict:
    return await Evaluator(conn).evaluate(conversation_id, **kw)
