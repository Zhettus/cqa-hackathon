"""Экспорт результатов в формате организаторов и проверка файла.

    python scripts/export_results.py --group DEV --out results/dev [--version 1] [--team "Команда"] [--db data/cqa.db]
    python scripts/export_results.py --group <prod_group_id> --out results/prod

Берётся автоматическая оценка (без апелляций) по заданной версии чек-листа (по умолчанию — последняя в БД).
После записи файл сразу проверяется (scripts/validate_results.py); код возврата 0 только при нуле ошибок.
Имя команды: --team, иначе TEAM_NAME из .env, иначе плейсхолдер из config/export_mapping.json (валидатор его отклонит).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import db  # noqa: E402
from backend.config import get_settings  # noqa: E402
from backend.export_results import export_group, transcript_lengths, validate_file, write_document  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", required=True, help="group_id набора (DEV или prod)")
    ap.add_argument("--out", required=True, help="каталог: results/dev | results/prod (файл results.json)")
    ap.add_argument("--db", default=None)
    ap.add_argument("--checklist", default="chat-quality")
    ap.add_argument("--version", type=int, default=None, help="замороженная версия чек-листа (по умолчанию последняя)")
    ap.add_argument("--team", default=None)
    ap.add_argument("--no-validate", action="store_true")
    args = ap.parse_args()
    conn = db.connect(args.db or get_settings().db_path)
    db.init_db(conn)
    doc = export_group(conn, args.group, checklist_id=args.checklist, checklist_version=args.version, team_name=args.team)
    path = write_document(doc, Path(args.out))
    ok = sum(r["status"] == "ok" for r in doc["results"])
    err = [r for r in doc["results"] if r["status"] == "error"]
    print(f"{path}: записей {len(doc['results'])}, ok {ok}, error {len(err)}; model {doc['model']['name']} ({doc['model']['mode']}); "
          f"team_name {doc['team_name']!r}")
    for r in err:
        print(f"  error {r['conversation_id']}: {r['error'][:160]}")
    if args.no_validate:
        return 0
    cl = db.get_checklist(conn, args.checklist, args.version)
    errors = validate_file(path, lengths=transcript_lengths(db.list_conversations(conn, args.group)), checklist=cl)
    for e in errors:
        print("  ✗", e)
    print(f"Проверка формата: ошибок {len(errors)}" + ("" if errors else " — можно сдавать"))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
