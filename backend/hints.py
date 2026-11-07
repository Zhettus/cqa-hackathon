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
