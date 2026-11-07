"""Единственное место расчёта баллов (инвариант 2). Строго по docs/SPEC.md.

- binary: «yes» = вес, «no» = 0.
- scale: points = value / max_step * weight.
- «na» исключает критерий; total = sum(points) / sum(весов применимых) * 100.
- Нет применимых критериев с весом → total = None. Секции — так же, по своим критериям.
- flag баллов не даёт, возвращается отдельно.
- Округление Decimal ROUND_HALF_UP до settings.rounding_decimals (по умолчанию 2).
  Округляются только выводимые значения; промежуточные расчёты точные.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Union

Answer = Union[str, int]
YES, NO, NA = "yes", "no", "na"
DEFAULT_ROUNDING_DECIMALS = 2


class AnswerError(ValueError):
    """Недопустимый ответ по критерию (тип, шкала, N/A)."""

    def __init__(self, criterion_id: str, code: str, message: str):
        super().__init__(f"{criterion_id}: {message}")
        self.criterion_id = criterion_id
        self.code = code


@dataclass(frozen=True)
class CriterionScore:
    criterion_id: str
    section: str
    type: str
    weight: Decimal
    answer: Answer
    applicable: bool          # False для «na» и для flag
    points: Decimal | None    # None для flag и «na»


@dataclass(frozen=True)
class SectionScore:
    id: str
    title: str
    score: Decimal | None     # None, если в секции нет применимых критериев с весом
    points: Decimal
    max_points: Decimal


@dataclass(frozen=True)
class FlagResult:
    criterion_id: str
    title: str
    answer: str


@dataclass(frozen=True)
class ScoreResult:
    total: Decimal | None
    sections: list[SectionScore]
    criteria: list[CriterionScore]
    flags: list[FlagResult] = field(default_factory=list)


def _dec(x: Any) -> Decimal:
    if isinstance(x, bool):
        raise TypeError("bool не является числом")
    return x if isinstance(x, Decimal) else Decimal(str(x))


def _scale_values(criterion: dict) -> list[Decimal]:
    return [_dec(s["value"]) for s in criterion.get("scale") or []]


def validate_answer(criterion: dict, answer: Any) -> Answer:
    """Проверяет ответ по типу критерия. Возвращает нормализованный ответ."""
    cid, ctype = criterion["id"], criterion["type"]
    if isinstance(answer, str):
        answer = answer.strip().lower()
    if answer == NA:
        if not criterion.get("na_allowed", False):
            raise AnswerError(cid, "na_not_allowed", "N/A для этого критерия не разрешён")
        return NA
    if ctype in ("binary", "flag"):
        if answer not in (YES, NO):
            raise AnswerError(cid, "invalid_binary_value", f"ожидается yes/no, получено {answer!r}")
        return answer
    if ctype == "scale":
        if isinstance(answer, bool) or not isinstance(answer, (int, float, Decimal)):
            raise AnswerError(cid, "invalid_scale_value", f"ожидается ступень шкалы, получено {answer!r}")
        if _dec(answer) not in _scale_values(criterion):
            raise AnswerError(cid, "invalid_scale_value",
                              f"{answer!r} не является ступенью шкалы {[int(v) if v == v.to_integral() else v for v in _scale_values(criterion)]}")
        return int(answer) if _dec(answer) == _dec(answer).to_integral() else answer
    raise AnswerError(cid, "unknown_type", f"неизвестный тип критерия {ctype!r}")


def criterion_points(criterion: dict, answer: Answer) -> Decimal | None:
    """Баллы по одному критерию; None для «na» и для flag."""
    if answer == NA or criterion["type"] == "flag":
        return None
    weight = _dec(criterion["weight"])
    if criterion["type"] == "binary":
        return weight if answer == YES else Decimal(0)
    max_step = max(_scale_values(criterion))
    if max_step == 0:
        return Decimal(0)
    return _dec(answer) / max_step * weight


def _ratio(points: Decimal, weights: Decimal, q: Decimal) -> Decimal | None:
    if weights == 0:
        return None
    return (points / weights * 100).quantize(q, rounding=ROUND_HALF_UP)


def score(checklist: dict, answers: dict[str, Any]) -> ScoreResult:
    """Считает итог, секции и флаги. Ответ нужен по каждому критерию чек-листа."""
    decimals = int((checklist.get("settings") or {}).get("rounding_decimals", DEFAULT_ROUNDING_DECIMALS))
    q = Decimal(1).scaleb(-decimals)

    known = {c["id"] for c in checklist["criteria"]}
    extra = sorted(set(answers) - known)
    if extra:
        raise AnswerError(extra[0], "unknown_criterion", "критерия нет в чек-листе")

    results: list[CriterionScore] = []
    flags: list[FlagResult] = []
    for c in checklist["criteria"]:
        if c["id"] not in answers:
            raise AnswerError(c["id"], "missing_criterion", "нет ответа по критерию")
        ans = validate_answer(c, answers[c["id"]])
        pts = criterion_points(c, ans)
        applicable = ans != NA and c["type"] != "flag"
        results.append(CriterionScore(c["id"], c["section"], c["type"], _dec(c["weight"]), ans, applicable, pts))
        if c["type"] == "flag":
            flags.append(FlagResult(c["id"], c.get("title", ""), ans))

    def aggregate(items: list[CriterionScore]) -> tuple[Decimal, Decimal]:
        app = [r for r in items if r.applicable]
        return sum((r.points for r in app), Decimal(0)), sum((r.weight for r in app), Decimal(0))

    sections = []
    for s in checklist["sections"]:
        pts, wts = aggregate([r for r in results if r.section == s["id"]])
        sections.append(SectionScore(s["id"], s.get("title", ""), _ratio(pts, wts, q),
                                     pts.quantize(q, rounding=ROUND_HALF_UP), wts))
    total_pts, total_wts = aggregate(results)
    return ScoreResult(_ratio(total_pts, total_wts, q), sections, results, flags)


def to_jsonable(result: ScoreResult) -> dict:
    """Decimal → float для JSON-ответов API."""
    f = lambda d: None if d is None else float(d)  # noqa: E731
    return {
        "total": f(result.total),
        "sections": [{"id": s.id, "title": s.title, "score": f(s.score),
                      "points": f(s.points), "max_points": f(s.max_points)} for s in result.sections],
        "criteria": [{"criterion_id": c.criterion_id, "section": c.section, "type": c.type,
                      "weight": f(c.weight), "answer": c.answer, "applicable": c.applicable,
                      "points": f(c.points)} for c in result.criteria],
        "flags": [{"criterion_id": fl.criterion_id, "title": fl.title, "answer": fl.answer} for fl in result.flags],
    }
