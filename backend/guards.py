"""Жёсткие правила поверх ответа модели. Задаются в конфиге критерия полем "guards":

  {"id": "c06_timing",
   "when": {"any": [{"fact": "first_response_s", "op": ">", "value_from": "settings.first_response_threshold_s"},
                    {"fact": "max_unwarned_gap_s", "op": ">", "value_from": "settings.pause_threshold_s"}]},
   "max_answer": 7,
   "note": "Первая реакция или пауза без предупреждения превышает порог (R7)"}

Условия: when.any / when.all — списки {fact, op, value | value_from}; value_from — "settings.X" или "facts.X".
Операторы: > >= < <= == != is_null not_null. Действия: max_answer, min_answer (scale — ближайшая
допустимая ступень), force_answer (любой тип). Срабатывание возвращается для записи в обоснование.
"""
from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Any

OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le,
       "==": operator.eq, "!=": operator.ne}


class GuardConfigError(ValueError):
    pass


@dataclass(frozen=True)
class GuardHit:
    guard_id: str
    note: str
    conditions: list[str]        # человекочитаемо: «first_response_s = 311 > 180»
    before: Any
    after: Any

    @property
    def changed(self) -> bool:
        return self.before != self.after

    def explanation(self) -> str:
        cond = "; ".join(self.conditions)
        if self.changed:
            return f"[Правило {self.guard_id}] {self.note} ({cond}): ответ модели {self.before} → {self.after}."
        return f"[Правило {self.guard_id}] {self.note} ({cond}): ответ модели {self.before} уже соответствует правилу."


def _resolve(ref: str, facts: dict, settings: dict) -> Any:
    scope, _, key = ref.partition(".")
    src = {"settings": settings, "facts": facts}.get(scope)
    if src is None or not key:
        raise GuardConfigError(f"value_from должен начинаться с settings. или facts.: {ref!r}")
    if key not in src:
        raise GuardConfigError(f"{ref!r} не найден")
    return src[key]


def _check(cond: dict, facts: dict, settings: dict) -> tuple[bool, str]:
    name, op = cond.get("fact"), cond.get("op")
    if name not in facts:
        raise GuardConfigError(f"неизвестный факт {name!r}")
    actual = facts[name]
    if op == "is_null":
        return actual is None, f"{name} = null"
    if op == "not_null":
        return actual is not None, f"{name} = {actual}"
    if op not in OPS:
        raise GuardConfigError(f"неизвестный оператор {op!r}")
    expected = _resolve(cond["value_from"], facts, settings) if "value_from" in cond else cond.get("value")
    if actual is None or expected is None:
        return False, f"{name} = {actual}"
    ok = OPS[op](actual, expected)
    return ok, f"{name} = {_fmt(actual)} {op} {_fmt(expected)}"


def _fmt(v: Any) -> str:
    return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)


def _steps(criterion: dict) -> list[int]:
    return sorted(s["value"] for s in criterion.get("scale") or [])


def _apply_action(guard: dict, criterion: dict, answer: Any) -> Any:
    if "force_answer" in guard:
        return guard["force_answer"]
    if answer == "na" or criterion["type"] != "scale":
        return answer
    steps = _steps(criterion)
    if "max_answer" in guard and answer > guard["max_answer"]:
        allowed = [s for s in steps if s <= guard["max_answer"]]
        answer = max(allowed) if allowed else min(steps)
    if "min_answer" in guard and answer < guard["min_answer"]:
        allowed = [s for s in steps if s >= guard["min_answer"]]
        answer = min(allowed) if allowed else max(steps)
    return answer


def apply_guards(criterion: dict, answer: Any, facts: dict, settings: dict) -> tuple[Any, list[GuardHit]]:
    hits: list[GuardHit] = []
    for g in criterion.get("guards") or []:
        when = g.get("when") or {}
        mode = "all" if "all" in when else "any"
        conds = when.get(mode) or []
        if not conds:
            raise GuardConfigError(f"guard {g.get('id')}: нет условий")
        results = [_check(c, facts, settings) for c in conds]
        fired = all(r[0] for r in results) if mode == "all" else any(r[0] for r in results)
        if not fired:
            continue
        new = _apply_action(g, criterion, answer)
        hits.append(GuardHit(g.get("id", "guard"), g.get("note", ""), [r[1] for r in results if r[0]], answer, new))
        answer = new
    return answer, hits


# --- проверка конфига guards при сохранении версии чек-листа -------------------------------------
def validate_guards(criterion: dict, settings: dict) -> list[str]:
    """Ошибки конфигурации guards критерия (пусто — всё корректно). settings — уже с умолчаниями."""
    from backend.preprocess import Facts
    from backend.scoring import AnswerError, validate_answer

    fact_names = set(Facts.model_fields)
    guards = criterion.get("guards")
    if guards is None:
        return []
    if not isinstance(guards, list):
        return ["guards — список правил"]
    msgs: list[str] = []
    for k, g in enumerate(guards):
        gid = (g.get("id") if isinstance(g, dict) else None) or f"#{k}"
        if not isinstance(g, dict):
            msgs.append(f"{gid}: правило — объект")
            continue
        when = g.get("when")
        conds = None
        if isinstance(when, dict):
            conds = when.get("all") if "all" in when else when.get("any")
        if not isinstance(conds, list) or not conds:
            msgs.append(f"{gid}: when.any или when.all — непустой список условий")
            conds = []
        for c in conds:
            if not isinstance(c, dict):
                msgs.append(f"{gid}: условие — объект {{fact, op, value|value_from}}")
                continue
            if c.get("fact") not in fact_names:
                msgs.append(f"{gid}: неизвестный факт {c.get('fact')!r}")
            op = c.get("op")
            if op not in OPS and op not in ("is_null", "not_null"):
                msgs.append(f"{gid}: неизвестный оператор {op!r}")
            if "value_from" in c:
                scope, _, key = str(c["value_from"]).partition(".")
                if scope == "settings":
                    if key not in settings:
                        msgs.append(f"{gid}: {c['value_from']!r} нет в settings и нет значения по умолчанию")
                elif scope == "facts":
                    if key not in fact_names:
                        msgs.append(f"{gid}: {c['value_from']!r} — неизвестный факт")
                else:
                    msgs.append(f"{gid}: value_from должен начинаться с settings. или facts.")
            elif op in OPS and "value" not in c:
                msgs.append(f"{gid}: нужно value или value_from")
        actions = [a for a in ("max_answer", "min_answer", "force_answer") if a in g]
        if not actions:
            msgs.append(f"{gid}: нужно действие max_answer, min_answer или force_answer")
        for a in ("max_answer", "min_answer"):
            if a in g:
                if criterion.get("type") != "scale":
                    msgs.append(f"{gid}: {a} допустим только для type=scale")
                elif isinstance(g[a], bool) or not isinstance(g[a], (int, float)):
                    msgs.append(f"{gid}: {a} — число")
        if "force_answer" in g:
            try:
                validate_answer(criterion, g["force_answer"])
            except AnswerError as e:
                msgs.append(f"{gid}: force_answer: {e}")
            except (KeyError, TypeError):
                msgs.append(f"{gid}: force_answer нельзя проверить — у критерия нет корректных id/type")
    return msgs
