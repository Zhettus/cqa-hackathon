"""Проверка results.json по формату организаторов. Нули в выводе — можно пушить.

    python scripts/validate_results.py results/dev/results.json --group DEV
    python scripts/validate_results.py results/prod/results.json --group <prod_group_id> --data data/prod_raw.json --expected-count 10

Правила: валидный UTF-8 JSON; schema_version/team_name/model; уникальные conversation_id, совпадающие с набором;
число записей (по умолчанию — размер набора); у ok-записей 8 кодов ровно по одному разу, допустимые значения,
непустые rationale и evidence_turns в пределах длины исходного транскрипта, final_score пересчитывается
из value нашим scoring и совпадает, critical_flag = value критерия critical; у error-записей criteria=[],
score=null, непустой error.
Набор берётся из --data (JSON организаторов; по умолчанию data/<group в нижнем регистре>.json), иначе из БД.
Чек-лист для пересчёта — из БД (--version; по умолчанию последняя) или из файла --checklist-file.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import db  # noqa: E402
from backend.config import get_settings  # noqa: E402
from backend.export_results import transcript_lengths, validate_file  # noqa: E402


def load_records(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw["data"]["records"] if isinstance(raw, dict) and "data" in raw else raw["records"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--group", required=True)
    ap.add_argument("--data", default=None, help="исходный JSON набора (по умолчанию data/<group>.json, если есть)")
    ap.add_argument("--db", default=None)
    ap.add_argument("--checklist", default="chat-quality")
    ap.add_argument("--version", type=int, default=None)
    ap.add_argument("--checklist-file", default=None)
    ap.add_argument("--expected-count", type=int, default=None, help="ожидаемое число записей (DEV 65, prod 10)")
    args = ap.parse_args()

    data = Path(args.data) if args.data else ROOT / "data" / f"{args.group.lower()}.json"
    conn = db.connect(args.db or get_settings().db_path)
    db.init_db(conn)
    if data.exists():
        lengths = transcript_lengths(load_records(data))
        source = str(data)
    else:
        lengths = transcript_lengths(db.list_conversations(conn, args.group))
        source = f"БД, набор {args.group}"
    if not lengths:
        print(f"набор {args.group} не найден ни в {data}, ни в БД", file=sys.stderr)
        return 2
    if args.checklist_file:
        cl = json.loads(Path(args.checklist_file).read_text(encoding="utf-8"))
    else:
        cl = db.get_checklist(conn, args.checklist, args.version)
    errors = validate_file(Path(args.path), lengths=lengths, checklist=cl, expected_count=args.expected_count)
    print(f"{args.path}: набор из {source} ({len(lengths)} диалогов), чек-лист {cl['id']} v{cl['version']}")
    for e in errors:
        print("  ✗", e)
    print(f"Ошибок: {len(errors)}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
