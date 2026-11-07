"""Разбивает eval/gold_draft.json по полю split на gold_tune.json и gold_holdout.json.

ВНИМАНИЕ (2026-11-07): gold_draft.json — архив; источник правды — gold_tune.json и gold_holdout.json,
правки эталона вносятся туда. Повторный запуск с --force перезапишет их содержимым архива.

Запуск: python scripts/split_gold.py [--src eval/gold_draft.json] [--force]

Формат сохраняется: если каждая аннотация в исходнике занимает одну строку,
строки копируются дословно (шапка и хвост файла тоже). Иначе — сериализация
в том же стиле: шапка с отступом 2, аннотация — компактный JSON в одну строку.
Скрипт печатает только счётчики, содержимое holdout не выводится.
Существующие файлы не перезаписываются без --force (eval/*.json — разметка людей).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

OUTPUTS = {"tune": "gold_tune.json", "holdout": "gold_holdout.json"}


def render(doc: dict) -> str:
    """Сериализация в стиле gold_draft.json."""
    head = {k: v for k, v in doc.items() if k != "annotations"}
    lines = ["{"]
    for k, v in head.items():
        lines.append(f"  {json.dumps(k, ensure_ascii=False)}: {json.dumps(v, ensure_ascii=False)},")
    rows = [json.dumps(a, ensure_ascii=False, separators=(",", ":")) for a in doc["annotations"]]
    lines.append('  "annotations": [')
    lines.append(",\n".join(rows))
    lines.append("  ]")
    lines.append("}")
    return "\n".join(lines) + "\n"


def split_lines(text: str, doc: dict) -> dict[str, str] | None:
    """Дословное копирование: каждая аннотация — ровно одна строка исходника."""
    lines = text.split("\n")
    rows = [i for i, ln in enumerate(lines) if ln.lstrip().startswith('{"conversation_id"')]
    if len(rows) != len(doc["annotations"]) or rows != list(range(rows[0], rows[0] + len(rows))):
        return None
    head, tail = lines[: rows[0]], lines[rows[-1] + 1 :]
    parsed = []
    for i in rows:
        raw = lines[i].rstrip().removesuffix(",")
        parsed.append((json.loads(raw).get("split"), raw))
    if [json.loads(r) for _, r in parsed] != doc["annotations"]:
        return None
    out = {}
    for name in OUTPUTS:
        body = [r for s, r in parsed if s == name]
        out[name] = "\n".join(head + [",\n".join(body)] + tail)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="eval/gold_draft.json")
    ap.add_argument("--force", action="store_true", help="перезаписать существующие файлы")
    args = ap.parse_args()

    src = Path(args.src)
    text = src.read_text(encoding="utf-8")
    doc = json.loads(text)
    verbatim = split_lines(text, doc)
    if verbatim is None:
        print("Раскладка исходника нестандартная: файлы будут пересериализованы.", file=sys.stderr)

    splits = Counter(a.get("split") for a in doc["annotations"])
    unknown = set(splits) - set(OUTPUTS)
    if unknown:
        print(f"Ошибка: неизвестные значения split: {sorted(map(str, unknown))}", file=sys.stderr)
        return 1

    targets = {name: src.parent / fname for name, fname in OUTPUTS.items()}
    existing = [str(p) for p in targets.values() if p.exists()]
    if existing and not args.force:
        print(f"Ошибка: файлы уже существуют: {existing}. Используйте --force.", file=sys.stderr)
        return 1

    for name, path in targets.items():
        part = dict(doc)
        part["annotations"] = [a for a in doc["annotations"] if a.get("split") == name]
        content = verbatim[name] if verbatim else render(part)
        if json.loads(content) != part:
            print(f"Ошибка: проверка содержимого {path} не прошла", file=sys.stderr)
            return 1
        path.write_text(content, encoding="utf-8")
        print(f"{path}: {len(part['annotations'])} записей")
    return 0


if __name__ == "__main__":
    sys.exit(main())
