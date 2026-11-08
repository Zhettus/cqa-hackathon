"""Оценить все диалоги набора текущей конфигурацией (.env) без сервера; упавшие повторить; показать время.

    python scripts/evaluate_group.py --group DEV --db data/cqa_local.db --parallel 1
    python scripts/evaluate_group.py --group <prod_group_id> --parallel 3 --retries 2

- Набор берётся из БД; если его там нет — импортируется из data/<group>.json (prefer local) или API организаторов.
- Версия чек-листа: --version, по умолчанию последняя в БД. Режим: --mode или EVAL_MODE из .env.
- Кэш оценок включён: диалог, уже оценённый этой же конфигурацией (чек-лист, промпт, модель, голоса), не пересчитывается.
- Ошибки провайдера (ключ, баланс) останавливают прогон; 429/503 — ожидание и повтор (RATE_LIMIT_*).
- После прохода failed повторяются --retries раз. Код возврата 0, если все диалоги completed или not_evaluable.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import data_source, db  # noqa: E402
from backend.config import get_settings  # noqa: E402
from backend.evaluator import Evaluator, default_votes  # noqa: E402
from backend.llm import LLMError, get_provider, retry_rate_limited  # noqa: E402
from backend.prompt import prompt_version  # noqa: E402


async def run(args) -> int:
    conn = db.connect(args.db or get_settings().db_path)
    db.init_db(conn)
    if not db.list_conversations(conn, args.group):
        info = data_source.import_group(conn, args.group, prefer=args.prefer)
        print(f"импорт {args.group}: {info}", flush=True)
    ids = [c["conversation_id"] for c in db.list_conversations(conn, args.group)]
    if args.ids:
        ids = [i for i in ids if i in set(args.ids)]
    checklist = db.get_checklist(conn, args.checklist, args.version)
    provider = get_provider()
    votes = args.votes or default_votes()
    evaluator = Evaluator(conn, provider, mode=args.mode)
    print(f"Набор {args.group}: {len(ids)} диалогов; модель {provider.name}/{provider.model}; чек-лист "
          f"{checklist['id']} v{checklist['version']}; промпт {prompt_version(mode=evaluator.mode)}; режим {evaluator.mode}; "
          f"голосов {votes}; параллельно {args.parallel}", flush=True)
    sem = asyncio.Semaphore(args.parallel)
    t0 = time.time()
    statuses: dict[str, dict] = {}

    async def one(cid: str, attempt: int):
        async with sem:
            t1 = time.time()
            try:
                ev = await retry_rate_limited(
                    lambda: evaluator.evaluate(cid, checklist["id"], checklist["version"], votes=votes, use_cache=True),
                    on_wait=lambda s, n: print(f"  {cid}: лимит/перегрузка провайдера, ожидание {s:.0f} с (попытка {n})", flush=True))
            except LLMError as e:
                if e.code in ("auth", "billing", "rate_limit"):
                    raise
                ev = {"status": "failed", "status_reason": str(e), "total": None}
            statuses[cid] = ev
            took = time.time() - t1
            tag = " (кэш)" if ev.get("cached") else f" ({took:.0f} с)"
            extra = f" — {ev.get('status_reason', '')[:140]}" if ev["status"] == "failed" else ""
            print(f"  [{len(statuses)}/{len(ids)}] {cid}: {ev['status']} {ev.get('total')}{tag}{extra}", flush=True)

    try:
        await asyncio.gather(*(one(c, 1) for c in ids))
        for r in range(1, args.retries + 1):
            failed = [c for c, ev in statuses.items() if ev["status"] == "failed"]
            if not failed:
                break
            print(f"Повтор {r}: {len(failed)} failed — {', '.join(failed)}", flush=True)
            await asyncio.gather(*(one(c, r + 1) for c in failed))
    except LLMError as e:
        what = {"auth": "ключ провайдера не принят", "billing": "баланс/квота провайдера исчерпаны",
                "rate_limit": "лимит запросов не снят после повторов"}.get(e.code, e.code)
        print(f"Остановлено ({what}): {e}. Оценённые диалоги сохранены в БД; повторный запуск продолжит с кэшем.",
              file=sys.stderr, flush=True)
        return 3
    total = time.time() - t0
    by = {}
    for ev in statuses.values():
        by[ev["status"]] = by.get(ev["status"], 0) + 1
    fresh = [c for c, ev in statuses.items() if not ev.get("cached")]
    print(f"\nГотово за {total:.0f} с ({total / 60:.1f} мин): {by}; новых оценок {len(fresh)}"
          + (f", ≈{total / len(fresh):.0f} с на новый диалог" if fresh else ""), flush=True)
    bad = [c for c, ev in statuses.items() if ev["status"] == "failed"]
    if bad:
        print(f"failed после повторов: {bad}", flush=True)
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", required=True)
    ap.add_argument("--db", default=None)
    ap.add_argument("--checklist", default="chat-quality")
    ap.add_argument("--version", type=int, default=None)
    ap.add_argument("--mode", default=None, choices=["per_criterion", "per_dialog"])
    ap.add_argument("--votes", type=int, default=None)
    ap.add_argument("--parallel", type=int, default=1)
    ap.add_argument("--retries", type=int, default=1, help="сколько раз повторить упавшие")
    ap.add_argument("--prefer", default="local", choices=["local", "remote"], help="источник при импорте набора")
    ap.add_argument("--ids", nargs="*")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
