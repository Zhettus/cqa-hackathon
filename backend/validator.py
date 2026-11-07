"""Проверка ответа модели по критерию (инвариант 4). Ошибка → повтор с текстом ошибки."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from backend.preprocess import Preprocessed
from backend.scoring import AnswerError, validate_answer

QUOTE_MIN_RATIO = 0.85


@dataclass(frozen=True)
class ValidationIssue:
    code: str          # bad_shape | na_not_allowed | invalid_scale_value | invalid_binary_value |
                       # unknown_idx | quote_not_found | missing_na_reason | missing_evidence | bad_range
    message: str

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower().replace("ё", "е")
    s = re.sub(r"[«»“”„\"'`’]", "", s)
    return re.sub(r"\s+", " ", s).strip(" .,!?;:…-")


def quote_ratio(quote: str, text: str) -> float:
    """Лучшее совпадение цитаты с подстрокой текста (0..1)."""
    q, t = _norm(quote), _norm(text)
    if not q:
        return 0.0
    if q in t:
        return 1.0
    if len(q) >= len(t):
        return SequenceMatcher(None, q, t).ratio()
    best, n = 0.0, len(q)
    for width in {n - 2, n, n + 2}:
        if width <= 0 or width > len(t):
            continue
        for i in range(0, len(t) - width + 1):
            r = SequenceMatcher(None, q, t[i:i + width]).ratio()
            if r > best:
                best = r
                if best >= 0.999:
                    return best
    return best


_QUOTED = re.compile(r"«[^»]*»|\"[^\"]*\"|“[^”]*”")


def is_russian(text: str) -> bool:
    """Обоснование на русском: кириллицы вне цитат не меньше латиницы."""
    bare = _QUOTED.sub(" ", text)
    cyr = len(re.findall(r"[а-яёА-ЯЁ]", bare))
    lat = len(re.findall(r"[a-zA-Z]", bare))
    return cyr >= lat


def validate_response(criterion: dict, resp: dict, pre: Preprocessed) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for key in ("evidence", "checked_range", "na_reason", "reasoning", "answer"):
        if key not in resp:
            issues.append(ValidationIssue("bad_shape", f"нет поля {key}"))
    if issues:
        return issues

    try:
        answer = validate_answer(criterion, resp["answer"])
    except AnswerError as e:
        return [ValidationIssue(e.code, str(e))]

    texts = {m.idx: m.text for m in pre.messages}
    evidence = resp.get("evidence") or []
    if not isinstance(evidence, list):
        return [ValidationIssue("bad_shape", "evidence должен быть списком")]
    for ev in evidence:
        idx, quote = ev.get("idx"), ev.get("quote", "")
        if idx not in texts:
            issues.append(ValidationIssue("unknown_idx", f"реплики #{idx} нет в транскрипте (1..{len(texts)})"))
            continue
        r = quote_ratio(quote, texts[idx])
        if r < QUOTE_MIN_RATIO:
            issues.append(ValidationIssue(
                "quote_not_found",
                f"цитата «{quote}» не найдена в тексте реплики #{idx} (совпадение {r:.2f} < {QUOTE_MIN_RATIO}); "
                f"текст реплики: «{texts[idx]}»"))

    rng = resp.get("checked_range")
    if rng is not None:
        a, b = rng.get("from"), rng.get("to")
        if a not in texts or b not in texts or a > b:
            issues.append(ValidationIssue("bad_range", f"checked_range {a}–{b} вне транскрипта (1..{len(texts)})"))

    if answer == "na":
        if not (resp.get("na_reason") or "").strip():
            issues.append(ValidationIssue("missing_na_reason", "при answer=na нужно na_reason: какое условие выполнено"))
    elif not evidence and rng is None:
        issues.append(ValidationIssue("missing_evidence",
                                      "нужны evidence (idx + цитата) или checked_range (где действие ожидалось)"))
    reasoning = (resp.get("reasoning") or "").strip()
    if not reasoning:
        issues.append(ValidationIssue("bad_shape", "пустое reasoning"))
    elif not is_russian(reasoning):
        issues.append(ValidationIssue("reasoning_language",
                                      "reasoning должно быть на русском (цитаты — на языке оригинала в «»)"))
    return issues


def feedback_text(issues: list[ValidationIssue], raw: str) -> str:
    lines = "\n".join(f"- {i.message}" for i in issues)
    return ("ПРЕДЫДУЩИЙ ОТВЕТ ОТКЛОНЁН ВАЛИДАТОРОМ:\n" + lines +
            f"\nПредыдущий ответ: {raw}\nИсправь ошибки и верни JSON заново. Цитаты копируй дословно из текста реплики.")
