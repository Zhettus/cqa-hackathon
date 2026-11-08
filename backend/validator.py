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


_QUOTE_CHARS = set("«»“”„\"'`’")
_STRIP_CHARS = " .,!?;:…-"


def _norm_map(s: str) -> tuple[str, list[int]]:
    """Нормализованный текст (NFKC, нижний регистр, ё→е, без кавычек, пробелы схлопнуты, края без
    пунктуации) и для каждого его символа — индекс символа в ИСХОДНОЙ строке."""
    out: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(s):
        for c in unicodedata.normalize("NFKC", ch).lower().replace("ё", "е"):
            if c in _QUOTE_CHARS:
                continue
            if c.isspace():
                if out and out[-1] == " ":
                    continue
                c = " "
            out.append(c)
            idx.append(i)
    a, b = 0, len(out)
    while a < b and out[a] in _STRIP_CHARS:
        a += 1
    while b > a and out[b - 1] in _STRIP_CHARS:
        b -= 1
    return "".join(out[a:b]), idx[a:b]


def match_quote(quote: str, text: str) -> tuple[float, str]:
    """Лучшее совпадение цитаты с подстрокой текста: (похожесть 0..1, эта подстрока ИСХОДНОГО текста).
    Сравнение нечёткое (регистр, ё/е, кавычки, пробелы, опечатки), но возвращаемая подстрока — всегда
    дословный фрагмент оригинала: именно она хранится как доказательство."""
    q, _ = _norm_map(quote)
    t, idx = _norm_map(text)
    if not q or not t:
        return 0.0, ""

    def orig(a: int, b: int) -> str:
        return text[idx[a]: idx[b - 1] + 1].strip()

    pos = t.find(q)
    if pos >= 0:
        return 1.0, orig(pos, pos + len(q))
    if len(q) >= len(t):
        return SequenceMatcher(None, q, t).ratio(), orig(0, len(t))
    best, span, n = 0.0, (0, len(t)), len(q)
    for width in sorted({n - 2, n, n + 2}):
        if width <= 0 or width > len(t):
            continue
        for i in range(0, len(t) - width + 1):
            r = SequenceMatcher(None, q, t[i:i + width]).ratio()
            if r > best:
                best, span = r, (i, i + width)
                if best >= 0.999:
                    return best, orig(*span)
    return best, orig(*span)


def quote_ratio(quote: str, text: str) -> float:
    """Лучшее совпадение цитаты с подстрокой текста (0..1)."""
    return match_quote(quote, text)[0]


def resolve_evidence(resp: dict, pre: Preprocessed) -> list[dict]:
    """После успешной валидации: доказательства с цитатой-подстрокой оригинала и формой модели.
    [{idx, quote: <подстрока text реплики idx>, model_quote: <как написала модель>}]"""
    texts = {m.idx: m.text for m in pre.messages}
    out = []
    for ev in resp.get("evidence") or []:
        _, sub = match_quote(ev["quote"], texts[ev["idx"]])
        out.append({"idx": ev["idx"], "quote": sub, "model_quote": ev["quote"]})
    return out


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
            if (resp.get("reasoning") or "").strip():
                # Разрешённый N/A, na_reason пуст, а условие объяснено в reasoning (так отвечает локальная модель):
                # берём причину оттуда и помечаем флагом na_reason_derived (решение пользователя, 2026-11-08).
                resp["na_reason"] = resp["reasoning"].strip()
                resp["na_reason_derived"] = True
            else:
                issues.append(ValidationIssue("missing_na_reason",
                                              "при answer=na нужно na_reason: какое условие выполнено"))
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
