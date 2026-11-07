"""Прогон оценки реальной моделью и сравнение с эталоном.

  python -m eval.run --gold eval/gold_tune.json                       # все диалоги эталона
  python -m eval.run --gold eval/gold_tune.json --gold eval/edge_cases.json --ids p1-ru-022 edge-ru-001
  python -m eval.run --ids p1-ru-009 --votes 3                         # без эталона — только результат

Диалоги грузятся из data/{dev,edge}.json в БД (DB_PATH). Эталон берётся только из переданных --gold;
для id, которых в них нет, выводится результат модели без сравнения. Отчёт: data/eval_reports/*.json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from backend import data_source, db
from backend.config import get_settings
from backend.evaluator import Evaluator
from backend.llm import LLMError, get_provider

ROOT = Path(__file__).resolve().parent.parent


def fmt(v) -> str:
    return "—" if v is None else str(v)


def load_gold(paths: list[str]) -> dict[str, dict]:
    gold = {}
    for p in paths:
        for a in json.loads(Path(p).read_text(encoding="utf-8"))["annotations"]:
            gold[a["conversation_id"]] = {**a, "_source": Path(p).name}
    return gold


def print_result(ev: dict, g: dict | None) -> dict:
    cid = ev["conversation_id"]
    stats = {"conversation_id": cid, "status": ev["status"], "total": ev["total"],
             "gold_total": g.get("total") if g else None, "match": 0, "compared": 0}
    src = f"эталон: {g['_source']}" if g else "эталон: нет (диалог не из переданных файлов)"
    print(f"\n=== {cid}  [{ev['status']}]  {src}")
    if ev["status"] != "completed":
        print(f"    {ev.get('status_reason')}")
        if g:
            exp = g.get("expected_behavior") or ("not_evaluable" if g.get("answers") is None else "completed")
            print(f"    ожидалось: {exp}, итог эталона {fmt(g.get('total'))}")
            stats["status_ok"] = exp == ev["status"]
        return stats
    ga = (g or {}).get("answers") or {}
    print(f"    {'крит.':<6}{'модель':>8}{'эталон':>8}  {'':2}{'увер.':>6}  обоснование")
    for c in ev["criteria"]:
        gv = ga.get(c["criterion_id"])
        mark = ""
        if gv is not None:
            stats["compared"] += 1
            same = gv == c["answer"]
            stats["match"] += same
            mark = "✓" if same else "✗"
        guard = " [guard]" if any(h["before"] != h["after"] for h in c.get("guards", [])) else ""
        expl = (c["explanation"] or "").replace("\n", " ")
        print(f"    {c['criterion_id']:<6}{fmt(c['answer']):>8}{fmt(gv):>8}  {mark:2}{fmt(c.get('confidence')):>6}  "
              f"{expl[:150]}{guard}")
    tot = f"{ev['total']}" + (f"  эталон {g['total']}" if g else "")
    cost = ev["cost"]
    print(f"    итог: {tot}   вызовов {cost['calls']} (отклонено валидатором {cost['rejected_calls']}), "
          f"токены {cost['tokens_in']}/{cost['tokens_out']}, ${cost['usd']:.4f}")
    stats.update(cost=cost)
    return stats


async def main_async(args) -> int:
    s = get_settings()
    conn = db.connect(args.db or s.db_path)
    db.init_db(conn)
    for gid in args.groups:
        data_source.import_group(conn, gid, prefer="local")
    gold = load_gold(args.gold)
    ids = args.ids or list(gold)
    if not ids:
        print("Нечего оценивать: укажите --gold или --ids", file=sys.stderr)
        return 2
    try:
        provider = get_provider()
    except Exception as e:  # noqa: BLE001
        print(f"Провайдер LLM не настроен: {e}. Заполните .env (см. .env.example).", file=sys.stderr)
        return 2
    print(f"Модель: {provider.name}/{provider.model}; голосов: {args.votes or 'VOTES из .env'}; диалогов: {len(ids)}")
    ev_ = Evaluator(conn, provider)
    t0 = time.time()
    sem = asyncio.Semaphore(args.parallel)

    async def one(cid):
        async with sem:
            try:
                return await ev_.evaluate(cid, args.checklist, args.version, votes=args.votes)
            except LLMError as e:
                return {"conversation_id": cid, "status": "failed", "status_reason": str(e), "total": None}

    # пробный диалог: при отсутствии доступа к модели останавливаемся сразу, а не на каждом диалоге
    first = await one(ids[0])
    if first["status"] == "failed" and "учётных данных" in (first.get("status_reason") or "") \
            or "доступ запрещён" in (first.get("status_reason") or ""):
        print(f"\nОстановлено: {first['status_reason']}", file=sys.stderr)
        return 3
    results = [first] + list(await asyncio.gather(*(one(c) for c in ids[1:])))
    rows = [print_result(r, gold.get(r["conversation_id"])) if "criteria" in r or r["status"] != "completed"
            else None for r in results]
    rows = [r for r in rows if r]
    compared = sum(r["compared"] for r in rows)
    match = sum(r["match"] for r in rows)
    with_total = [r for r in rows if r["gold_total"] is not None and r["total"] is not None]
    usd = sum((r.get("cost") or {}).get("usd", 0) for r in rows)
    print("\n=== СВОДКА")
    if compared:
        print(f"Совпадение по критериям: {match}/{compared} = {match / compared:.1%}")
    if with_total:
        mae = sum(abs(r["total"] - r["gold_total"]) for r in with_total) / len(with_total)
        exact = sum(abs(r["total"] - r["gold_total"]) < 0.005 for r in with_total)
        print(f"Итог: точное совпадение {exact}/{len(with_total)}, средняя абсолютная ошибка {mae:.2f}")
    print(f"Стоимость: ${usd:.4f}; время {time.time() - t0:.0f} с")
    out = ROOT / "data" / "eval_reports"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"run_{datetime.now():%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps({"ids": ids, "gold": args.gold, "results": results}, ensure_ascii=False, indent=1,
                               default=str), encoding="utf-8")
    print(f"Отчёт: {path.relative_to(ROOT)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gold", action="append", default=[], help="файл эталона (можно несколько)")
    ap.add_argument("--ids", nargs="*", help="conversation_id для оценки (по умолчанию — все из эталона)")
    ap.add_argument("--votes", type=int, default=None)
    ap.add_argument("--checklist", default="chat-quality")
    ap.add_argument("--version", type=int, default=None)
    ap.add_argument("--groups", nargs="*", default=["DEV", "EDGE"], help="наборы для загрузки из data/")
    ap.add_argument("--parallel", type=int, default=3, help="диалогов одновременно")
    ap.add_argument("--db", default=None)
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
