# Кандидаты версий чек-листа (не засеваются)

Файлы здесь НЕ подхватываются `db.init_db` — засеваются только `backend/checklists/default_v*.json`.

- `chat-quality_v2_reference.json` — v1 + поле `reference` у c04 (22 факта из `docs/knowledge.md`, без источников).
  Эксперимент `--votes 3` (отчёты `eval/reports/20261108_020705` edge, `20261108_021735` tune) — см. `docs/STATUS.md` §4.
  Активировать: `cp backend/checklists/candidates/chat-quality_v2_reference.json backend/checklists/default_v2.json`
  и перезапустить сервер (версия 2 станет последней и будет использоваться по умолчанию), либо
  `python scripts/build_reference_version.py --criterion c04 --db data/cqa.db`.
