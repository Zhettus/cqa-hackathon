"""Собирает версию чек-листа со справочником: поле reference критерия из docs/knowledge.md.

Запуск:
    python scripts/build_reference_version.py --criterion c04 [--knowledge docs/knowledge.md]
        [--out backend/checklists/default_v2.json] [--db data/cqa.db]

- Читает факты из knowledge.md (строки вида «N. факт — источники: …»), убирает ссылки на источники
  (в промпт они не передаются) и подставляет текст в criteria[<criterion>].reference.
- Пишет файл версии (по умолчанию следующий номер после последней default_v*.json) — он засевается
  в БД при старте (db.init_db). С --db сразу создаёт версию в указанной базе через insert_checklist_version
  (если такой версии ещё нет).
Код не знает про конкретный критерий: --criterion может быть любым id из чек-листа.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.checklist import CHECKLISTS_DIR, load_checklist_files, validate_checklist  # noqa: E402

_FACT = re.compile(r"^\s*\d+\.\s+(.*?)(?:\s+—\s+источник[и]?:.*)?$")


def reference_text(knowledge: Path) -> str:
    facts = []
    for line in knowledge.read_text(encoding="utf-8").splitlines():
        m = _FACT.match(line)
        if m and m.group(1).strip():
            facts.append(m.group(1).strip())
    if not facts:
        raise SystemExit(f"в {knowledge} не найдено фактов вида «N. текст»")
    return "\n".join(f"{i}. {t}" for i, t in enumerate(facts, 1))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--criterion", required=True)
    ap.add_argument("--knowledge", default=str(ROOT / "docs" / "knowledge.md"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--db", default=None)
    ap.add_argument("--comment", default="Справочник компании для критерия (reference)")
    args = ap.parse_args()

    files = load_checklist_files()
    if not files:
        raise SystemExit("нет файлов backend/checklists/default_v*.json")
    base = json.loads(json.dumps(files[-1]))
    new_version = int(base["version"]) + 1
    crit = next((c for c in base["criteria"] if c["id"] == args.criterion), None)
    if crit is None:
        raise SystemExit(f"критерия {args.criterion} нет в чек-листе")
    crit["reference"] = reference_text(Path(args.knowledge))
    base["version"] = new_version
    errors = validate_checklist(base)
    if errors:
        raise SystemExit(f"чек-лист невалиден: {errors}")
    out = Path(args.out) if args.out else CHECKLISTS_DIR / f"default_v{new_version}.json"
    out.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{out}: версия {new_version}, reference для {args.criterion}: {len(crit['reference'].splitlines())} фактов")
    if args.db:
        from backend import db
        conn = db.connect(args.db)
        db.init_db(conn)
        have = {v["version"] for v in db.list_checklist_versions(conn, base["id"])}
        if new_version in have:
            print(f"{args.db}: версия {new_version} уже есть")
        else:
            db.insert_checklist_version(conn, base, author="knowledge", comment=args.comment, version=new_version)
            print(f"{args.db}: версия {new_version} создана")
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
