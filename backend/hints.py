"""Регулярки-подсказки для критериев flag. Шаблоны берутся из конфига критерия
(поле "hints": [{"name", "pattern", "label"}]); ищутся только в засчитываемых репликах оператора.
Это подсказка модели, а не решение: совпадение может быть ложным."""
from __future__ import annotations

import re

from backend.preprocess import Preprocessed


def find_hints(criterion: dict, pre: Preprocessed) -> list[dict]:
    out = []
    for h in criterion.get("hints") or []:
        rx = re.compile(h["pattern"], re.I | re.U)
        for m in pre.messages:
            if not m.counts_for_operator:
                continue
            hit = rx.search(m.text)
            if hit:
                out.append({"idx": m.idx, "name": h["name"], "label": h.get("label", h["name"]),
                            "match": hit.group(0)})
    return out


def validate_hints(criterion: dict) -> list[str]:
    """Ошибки конфигурации hints (пусто — всё корректно): name и компилируемый pattern."""
    hints = criterion.get("hints")
    if hints is None:
        return []
    if not isinstance(hints, list):
        return ["hints — список шаблонов"]
    msgs = []
    for k, h in enumerate(hints):
        if not isinstance(h, dict) or not h.get("name") or not isinstance(h.get("pattern"), str):
            msgs.append(f"#{k}: шаблон — объект {{name, pattern[, label]}}")
            continue
        try:
            re.compile(h["pattern"], re.I | re.U)
        except re.error as e:
            msgs.append(f"{h['name']}: некорректная регулярка: {e}")
    return msgs
