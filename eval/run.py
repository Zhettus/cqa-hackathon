"""Оценка диалогов текущим чек-листом и сравнение с эталоном.

  python -m eval.run --gold eval/gold_tune.json
  python -m eval.run --gold eval/edge_cases.json --votes 3
  python -m eval.run --gold eval/gold_tune.json --ids p1-ru-022 p1-en-014

Оценки кэшируются по (диалог, версия чек-листа, версия промпта, модель, голоса): повторный запуск
без изменений промпта/чек-листа не вызывает модель. --no-cache — оценить заново.
Отчёт: eval/reports/<timestamp>.md (+ .json с сырыми данными).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from backend import data_source, db
from backend.scoring import score
from backend.config import get_settings
from backend.evaluator import Evaluator, default_votes
from backend.llm import LLMError, get_provider
from backend.preprocess import preprocess
from backend.prompt import prompt_version

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "eval" / "reports"
NOT_COMPUTED_FACTS = {"first_disclosure_at"}       # семантика — решает модель, не код
RECORD_FACTS = {"audio_url", "channel"}


# --- сравнение ---------------------------------------------------------------------------------
def step_error(criterion: dict, gold, sys_) -> int | None:
    """Ошибка в ступенях шкалы (|индекс эталона − индекс системы|); None, если одна из сторон N/A."""
    if gold == "na" or sys_ == "na":
        return None
    steps = sorted(s["value"] for s in criterion["scale"])
    return abs(steps.index(gold) - steps.index(sys_))


def cause_hint(crit: dict, row: dict, ev: dict, pre_msgs: dict) -> str:
    """Автоподсказка причины расхождения (для группировки; окончательно — разбор вручную)."""
    g, s = row["gold"], row["system"]
    facts = ev.get("facts") or {}
    tags = []
    if g == "na" or s == "na":
        tags.append("N/A")
    if row["guards"] or (crit.get("guards") and (facts.get("max_unwarned_gap_s") or 0) > 0):
        tags.append("время")
    if facts.get("client_language") and facts.get("operator_first_reply_language") \
            and facts["client_language"] != facts["operator_first_reply_language"]:
        tags.append("язык")
    cited = [q["n"] for q in (row["evidence"].get("quotes") or [])]
    if any(pre_msgs.get(n) and (pre_msgs[n].speaker in ("bot", "system") or
                                set(pre_msgs[n].flags) & {"client_not_hearing", "ivr", "after_end"}) for n in cited):
        tags.append("бот/удержание")
    return ", ".join(tags) or "трактовка или пропущенная реплика"


def compare(ev: dict, gold: dict, checklist: dict, record: dict) -> dict:
    crits = {c["id"]: c for c in checklist["criteria"]}
    res = {"conversation_id": ev["conversation_id"], "status": ev["status"], "language": record.get("language"),
           "channel": record.get("channel"), "total": ev.get("total"), "gold_total": gold.get("total"),
           "criteria": [], "uncertain": gold.get("uncertain") or [], "notes": gold.get("notes", ""),
           "cached": ev.get("cached", False), "cost": ev.get("cost") or {}, "status_reason": ev.get("status_reason")}
    expected_status = gold.get("expected_behavior") or ("not_evaluable" if gold.get("answers") is None else "completed")
    res["expected_status"] = expected_status
    res["status_mismatch"] = False
    if not gold.get("answers"):
        return res
    if ev["status"] != "completed":
        # Эталон ждёт ответы, а оценки нет (failed / not_evaluable): ошибка по всем критериям,
        # диалог остаётся в знаменателе точности и MAE (итог считается как 0).
        res["status_mismatch"] = True
        for cid, g in gold["answers"].items():
            if cid in crits:
                res["criteria"].append({"criterion_id": cid, "type": crits[cid]["type"], "gold": g,
                                        "system": ev["status"], "match": False, "status_mismatch": True,
                                        "explanation": ev.get("status_reason") or "", "guards": [], "evidence": {},
                                        "confidence": None, "uncertain": cid in res["uncertain"],
                                        "step_error": None, "cause": f"статус {ev['status']}"})
        return res
    pre = preprocess(record, checklist.get("settings") or {})
    msgs = {m.idx: m for m in pre.messages}
    by_id = {c["criterion_id"]: c for c in ev["criteria"]}
    accept = gold.get("accept") or {}          # {"c04": [0, 10]} — допустимые альтернативы эталону
    subs: dict[str, Any] = {}
    for cid, g in gold["answers"].items():
        c = by_id.get(cid)
        if c is None or cid not in crits:
            continue
        accepted = c["answer"] != g and c["answer"] in (accept.get(cid) or [])
        row = {"criterion_id": cid, "type": crits[cid]["type"], "gold": g, "system": c["answer"],
               "match": g == c["answer"] or accepted, "accepted": accepted,
               "explanation": c.get("explanation") or "",
               "guards": [h for h in c.get("guards", [])], "evidence": c.get("evidence") or {},
               "confidence": c.get("confidence"), "uncertain": cid in res["uncertain"]}
        row["step_error"] = (0 if accepted else step_error(crits[cid], g, c["answer"])) \
            if crits[cid]["type"] == "scale" else None
        if not row["match"]:
            row["cause"] = cause_hint(crits[cid], row, ev, msgs)
        if accepted:
            subs[cid] = c["answer"]
        res["criteria"].append(row)
    if subs:                                    # итог сравнивается с эталоном, пересчитанным под принятые ответы
        res["accepted"] = subs
        res["gold_total_listed"] = res["gold_total"]
        res["gold_total"] = float(score(checklist, {**gold["answers"], **subs}).total)
    return res


def edge_verdict(res: dict, gold: dict, record: dict, checklist: dict) -> dict:
    """Случай edge пройден, если: статус как ожидалось; вычисляемые факты совпали; совпали критерии из target
    (если среди target нет критериев — все критерии)."""
    problems = []
    if res["status"] != res["expected_status"]:
        problems.append(f"статус {res['status']}, ожидался {res['expected_status']}")
    facts = preprocess(record, checklist.get("settings") or {}).facts.model_dump()
    for k, v in (gold.get("facts") or {}).items():
        if k in NOT_COMPUTED_FACTS:
            continue
        actual = record.get(k) if k in RECORD_FACTS else facts.get(k)
        ok = abs(actual - v) <= 1 if isinstance(v, (int, float)) and isinstance(actual, (int, float)) else actual == v
        if not ok:
            problems.append(f"факт {k}: {actual} ≠ {v}")
    crit_ids = {c["id"] for c in checklist["criteria"]}
    targets = [t for t in gold.get("target", []) if t in crit_ids] or sorted(crit_ids)
    for row in res["criteria"]:
        if row["criterion_id"] in targets and not row["match"]:
            problems.append(f"{row['criterion_id']}: {row['system']} ≠ {row['gold']}")
    all_match = all(r["match"] for r in res["criteria"])
    return {"conversation_id": res["conversation_id"], "target": gold.get("target", []), "passed": not problems,
            "all_criteria_match": all_match, "problems": problems}


# --- метрики -----------------------------------------------------------------------------------
def metrics(results: list[dict], checklist: dict) -> dict:
    per = defaultdict(lambda: {"n": 0, "match": 0, "step_err": [], "na_mismatch": 0})
    groups = defaultdict(lambda: {"n": 0, "match": 0, "abs_err": [], "dialogs": 0})
    abs_err = []
    status_mismatch = []
    for r in results:
        if r.get("status_mismatch"):
            status_mismatch.append(r["conversation_id"])
        for row in r["criteria"]:
            p = per[row["criterion_id"]]
            p["n"] += 1
            p["match"] += row["match"]
            if row["type"] == "scale" and not row.get("status_mismatch"):
                if row["step_error"] is None and not row["match"]:
                    p["na_mismatch"] += 1
                elif row["step_error"] is not None:
                    p["step_err"].append(row["step_error"])
        if r["gold_total"] is None:
            err = None
        elif r["total"] is not None:
            err = abs(r["total"] - r["gold_total"])
        else:
            err = abs(r["gold_total"]) if r.get("status_mismatch") else None   # итог не получен → как 0
        if err is not None:
            abs_err.append(err)
        for key in (f"язык {r['language']}", f"канал {r['channel']}"):
            g = groups[key]
            g["dialogs"] += 1
            g["n"] += len(r["criteria"])
            g["match"] += sum(x["match"] for x in r["criteria"])
            if err is not None:
                g["abs_err"].append(err)
    n = sum(p["n"] for p in per.values())
    m = sum(p["match"] for p in per.values())
    return {"criteria": dict(per), "groups": dict(groups), "n": n, "match": m,
            "total_mae": sum(abs_err) / len(abs_err) if abs_err else None,
            "total_exact": sum(e < 0.005 for e in abs_err), "total_compared": len(abs_err),
            "status_mismatch": status_mismatch}


# --- отчёт -------------------------------------------------------------------------------------
def pct(a, b) -> str:
    return f"{a / b:.1%}" if b else "—"


def md_cell(v) -> str:
    return str(v).replace("|", "\\|").replace("\n", " ") if v is not None else "—"


def render(meta: dict, results: list[dict], mt: dict, edge: list[dict], checklist: dict) -> str:
    crits = {c["id"]: c for c in checklist["criteria"]}
    L = [f"# Прогон оценки {meta['started']}", "",
         f"- Эталон: {', '.join(meta['gold'])}",
         f"- Чек-лист: {checklist['id']} v{checklist['version']}; промпт: {meta['prompt_version']}",
         f"- Модель: {meta['model']}; голосов: {meta['votes']}; нормализация звонков: "
         f"{'да' if meta.get('normalize') else 'нет'}" + (f"; только канал {meta['channel']}" if meta.get("channel") else ""),
         f"- Диалогов: {len(results)}; из кэша: {sum(r['cached'] for r in results)}; "
         f"стоимость новых вызовов: ${meta['usd']:.4f}; время: {meta['seconds']:.0f} с", ""]
    bad = [r for r in results if r.get("status_mismatch")]
    if bad:
        L += [f"**Нет оценки при наличии эталона: {len(bad)}** (ошибка по всем критериям, итог считается как 0): " +
              "; ".join(f"{r['conversation_id']} — {r['status']}" + (f" ({r['status_reason']})" if r.get("status_reason") else "")
                        for r in bad), ""]
    L += ["## Итог", "",
          f"- Совпадение по критериям: **{mt['match']}/{mt['n']} = {pct(mt['match'], mt['n'])}**",
          f"- Диалогов без оценки при наличии эталона: {len(mt['status_mismatch'])}",
          f"- Итог: точное совпадение {mt['total_exact']}/{mt['total_compared']}, "
          f"средняя абсолютная ошибка **{mt['total_mae']:.2f}**" if mt["total_mae"] is not None else "- Итог: —", "",
          "## По критериям", "", "| Критерий | Тип | Совпадение | Ошибка в ступенях (среднее) | N/A ↔ значение |",
          "|---|---|---|---|---|"]
    for cid, c in crits.items():
        p = mt["criteria"].get(cid)
        if not p:
            continue
        se = f"{sum(p['step_err']) / len(p['step_err']):.2f}" if c["type"] == "scale" and p["step_err"] else "—"
        na = str(p["na_mismatch"]) if c["type"] == "scale" else "—"
        L.append(f"| {cid} {c['title']} | {c['type']} | {p['match']}/{p['n']} = {pct(p['match'], p['n'])} | {se} | {na} |")
    L += ["", "## По языкам и каналам", "", "| Группа | Диалогов | Совпадение по критериям | MAE итога |", "|---|---|---|---|"]
    for k in sorted(mt["groups"]):
        g = mt["groups"][k]
        mae = f"{sum(g['abs_err']) / len(g['abs_err']):.2f}" if g["abs_err"] else "—"
        L.append(f"| {k} | {g['dialogs']} | {g['match']}/{g['n']} = {pct(g['match'], g['n'])} | {mae} |")
    if edge:
        L += ["", "## Случаи edge", "", "| Диалог | Цель | Пройден | Все критерии | Проблемы |", "|---|---|---|---|---|"]
        for e in edge:
            L.append(f"| {e['conversation_id']} | {', '.join(e['target'])} | {'✅' if e['passed'] else '❌'} | "
                     f"{'да' if e['all_criteria_match'] else 'нет'} | {md_cell('; '.join(e['problems']) or '—')} |")
        L.append(f"\nПройдено: {sum(e['passed'] for e in edge)}/{len(edge)}")
    acc = [f"{r['conversation_id']}/{cid}={v}" for r in results for cid, v in (r.get("accepted") or {}).items()]
    if acc:
        L += ["", "**Принято как допустимая альтернатива (поле accept эталона):** " + ", ".join(acc)]
    rows = [(r, x) for r in results for x in r["criteria"] if not x["match"]]
    unc = [(r, x) for r, x in rows if x["uncertain"]]
    L += ["", f"## Расхождения ({len(rows)})", ""]
    if unc:
        L += ["**В записях с полем uncertain (возможна ошибка эталона):** " +
              ", ".join(f"{r['conversation_id']}/{x['criterion_id']}" for r, x in unc), ""]
    L += ["| Диалог | Язык/канал | Критерий | Эталон | Система | Автоподсказка причины | Guards | Reasoning |",
          "|---|---|---|---|---|---|---|---|"]
    for r, x in rows:
        guards = "; ".join(f"{g['id']}: {g['before']}→{g['after']}" for g in x["guards"]) or "—"
        mark = " ⚠️uncertain" if x["uncertain"] else ""
        L.append(f"| {r['conversation_id']}{mark} | {r['language']}/{r['channel']} | {x['criterion_id']} | "
                 f"{x['gold']} | {x['system']} | {x.get('cause', '')} | {md_cell(guards)} | {md_cell(x['explanation'])} |")
    L += ["", "## Заметки эталона по диалогам с расхождениями", ""]
    for cid in dict.fromkeys(r["conversation_id"] for r, _ in rows):
        r = next(x for x in results if x["conversation_id"] == cid)
        L.append(f"- **{cid}**: {r['notes']}")
    return "\n".join(L) + "\n"


# --- запуск ------------------------------------------------------------------------------------
async def main_async(args) -> int:
    s = get_settings()
    conn = db.connect(args.db or s.db_path)
    db.init_db(conn)
    for gid in args.groups:
        data_source.import_group(conn, gid, prefer="local")
    gold = {}
    for p in args.gold:
        for a in json.loads(Path(p).read_text(encoding="utf-8"))["annotations"]:
            gold[a["conversation_id"]] = a
    ids = args.ids or list(gold)
    if args.channel:
        chan = {c["conversation_id"]: c["channel"] for c in db.list_conversations(conn)}
        ids = [i for i in ids if chan.get(i) == args.channel]
    missing = [i for i in ids if i not in gold]
    if missing:
        print(f"Нет в эталоне {args.gold}: {missing}", file=sys.stderr)
        return 2
    checklist = db.get_checklist(conn, args.checklist, args.version)
    provider = get_provider()
    votes = args.votes or default_votes()
    evaluator = Evaluator(conn, provider, normalize_calls=args.normalize)
    print(f"Модель {provider.name}/{provider.model}, голосов {votes}, промпт {prompt_version()}, "
          f"чек-лист v{checklist['version']}, диалогов {len(ids)}, нормализация звонков: "
          f"{'да' if args.normalize else 'нет'}", flush=True)
    t0 = time.time()
    sem = asyncio.Semaphore(args.parallel)
    done = {"n": 0}

    async def one(cid):
        async with sem:
            try:
                ev = await evaluator.evaluate(cid, checklist["id"], checklist["version"], votes=votes,
                                              use_cache=not args.no_cache)
            except LLMError as e:
                if e.code == "auth":
                    raise
                ev = {"conversation_id": cid, "status": "failed", "status_reason": str(e), "total": None,
                      "criteria": []}
            except Exception as e:          # evaluator сам переводит сбои в failed; это страховка
                ev = {"conversation_id": cid, "status": "failed", "status_reason": f"{type(e).__name__}: {e}",
                      "total": None, "criteria": []}
            done["n"] += 1
            tag = " (кэш)" if ev.get("cached") else ""
            print(f"  [{done['n']}/{len(ids)}] {cid}: {ev['status']} {ev.get('total')}{tag}", flush=True)
            return ev

    try:
        evs = await asyncio.gather(*(one(c) for c in ids))
    except LLMError as e:
        print(f"Остановлено: {e}", file=sys.stderr)
        return 3
    records = {c["conversation_id"]: c for c in db.list_conversations(conn)}
    results = [compare(ev, gold[ev["conversation_id"]], checklist, records[ev["conversation_id"]]) for ev in evs]
    edge = [edge_verdict(r, gold[r["conversation_id"]], records[r["conversation_id"]], checklist)
            for r in results if "target" in gold[r["conversation_id"]]]
    mt = metrics(results, checklist)
    meta = {"started": datetime.now().strftime("%Y-%m-%d %H:%M"), "gold": args.gold,
            "prompt_version": prompt_version(), "model": f"{provider.name}/{provider.model}", "votes": votes,
            "normalize": bool(args.normalize), "channel": args.channel,
            "usd": sum((r["cost"] or {}).get("usd", 0) for r in results if not r["cached"]),
            "seconds": time.time() - t0}
    REPORTS.mkdir(parents=True, exist_ok=True)
    stem = REPORTS / datetime.now().strftime("%Y%m%d_%H%M%S")
    stem.with_suffix(".md").write_text(render(meta, results, mt, edge, checklist), encoding="utf-8")
    stem.with_suffix(".json").write_text(json.dumps({"meta": meta, "results": results, "edge": edge}, ensure_ascii=False,
                                                    indent=1, default=str), encoding="utf-8")
    print(f"\nСовпадение по критериям: {mt['match']}/{mt['n']} = {pct(mt['match'], mt['n'])}")
    if mt["status_mismatch"]:
        print(f"Без оценки при наличии эталона: {len(mt['status_mismatch'])} ({', '.join(mt['status_mismatch'])})")
    if mt["total_mae"] is not None:
        print(f"MAE итога: {mt['total_mae']:.2f}; точных итогов {mt['total_exact']}/{mt['total_compared']}")
    if edge:
        print(f"Edge пройдено: {sum(e['passed'] for e in edge)}/{len(edge)}")
    print(f"Стоимость новых вызовов: ${meta['usd']:.4f}; отчёт: {stem.with_suffix('.md').relative_to(ROOT)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gold", action="append", required=True, help="файл эталона (можно несколько раз)")
    ap.add_argument("--ids", nargs="*", help="только эти conversation_id из эталона")
    ap.add_argument("--votes", type=int, default=None, help="голосов на критерий (по умолчанию VOTES из .env)")
    ap.add_argument("--checklist", default="chat-quality")
    ap.add_argument("--version", type=int, default=None, help="версия чек-листа (по умолчанию текущая)")
    ap.add_argument("--groups", nargs="*", default=["DEV", "EDGE"], help="наборы для загрузки из data/")
    ap.add_argument("--parallel", type=int, default=4, help="диалогов одновременно")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--db", default=None)
    ap.add_argument("--normalize", action="store_true", help="нормализовать текст звонков моделью (этап 5)")
    ap.add_argument("--channel", default=None, help="только этот канал: 'voice call' | 'agent chat'")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
