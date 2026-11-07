"""Сводка замера локальной/бесплатной модели против Opus на тех же диалогах.

    python ollama/report_local.py --db data/cqa_local.db --local eval/reports/<edge>.json eval/reports/<tune>.json \
        --opus eval/reports/20261107_233008.json eval/reports/20261107_234106.json

Печатает: точность по критериям (локальная / Opus) на пересечении диалогов, итоги, MAE, число failed,
среднее время на диалог (evaluations.duration_ms), число повторов валидатора и средние токены.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path


def load(paths: list[str]) -> dict[str, dict]:
    out = {}
    for p in paths:
        d = json.loads(Path(p).read_text(encoding="utf-8"))
        for r in d["results"]:
            out[r["conversation_id"]] = {**r, "_mode": d["meta"].get("mode"), "_model": d["meta"].get("model")}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/cqa_local.db")
    ap.add_argument("--local", nargs="+", required=True)
    ap.add_argument("--opus", nargs="+", required=True)
    args = ap.parse_args()
    loc, opus = load(args.local), load(args.opus)
    ids = sorted(set(loc) & set(opus))
    per = defaultdict(lambda: {"loc": 0, "opus": 0, "n": 0})
    mae_l, mae_o, exact_l, exact_o, failed = [], [], 0, 0, []
    for cid in ids:
        a, b = loc[cid], opus[cid]
        if a["status"] != "completed":
            failed.append((cid, a["status"], (a.get("status_reason") or "")[:120]))
        ga = {c["criterion_id"]: c for c in a["criteria"]}
        for c in b["criteria"]:
            k = c["criterion_id"]
            per[k]["n"] += 1
            per[k]["opus"] += c["match"]
            per[k]["loc"] += bool(ga.get(k, {}).get("match"))
        if a["gold_total"] is not None:
            if a["total"] is not None:
                e = abs(a["total"] - a["gold_total"]); mae_l.append(e); exact_l += e < 0.005
            if b["total"] is not None:
                e = abs(b["total"] - b["gold_total"]); mae_o.append(e); exact_o += e < 0.005
    n = sum(v["n"] for v in per.values())
    L, O = sum(v["loc"] for v in per.values()), sum(v["opus"] for v in per.values())
    model = next(iter(loc.values()))["_model"]
    print(f"Диалогов в сравнении: {len(ids)}; модель {model}, режим {next(iter(loc.values()))['_mode']}")
    print(f"{'критерий':<6}{'локальная':>12}{'Opus':>10}")
    for k in sorted(per):
        v = per[k]; print(f"{k:<6}{v['loc']:>7}/{v['n']:<4}{v['opus']:>6}/{v['n']}")
    print(f"всего: локальная {L}/{n} = {L / n:.1%}; Opus {O}/{n} = {O / n:.1%}")
    if mae_l:
        print(f"MAE итога: локальная {sum(mae_l) / len(mae_l):.2f} (точных {exact_l}/{len(mae_l)}); "
              f"Opus {sum(mae_o) / len(mae_o):.2f} (точных {exact_o}/{len(mae_o)})")
    print(f"не оценено локальной (failed/другое): {len(failed)} {failed}")
    conn = sqlite3.connect(args.db)
    q = ",".join("?" * len(ids))
    row = conn.execute(f"SELECT AVG(duration_ms), MIN(duration_ms), MAX(duration_ms), COUNT(*) FROM evaluations "
                       f"WHERE status = 'completed' AND conversation_id IN ({q})", ids).fetchone()
    if row[3]:
        print(f"время на диалог (completed): среднее {row[0] / 1000:.0f} с, от {row[1] / 1000:.0f} до {row[2] / 1000:.0f} с, n={row[3]}")
    calls = conn.execute(f"SELECT status, COUNT(*), AVG(latency_ms), AVG(tokens_in), AVG(tokens_out) FROM llm_calls "
                         f"WHERE conversation_id IN ({q}) GROUP BY status", ids).fetchall()
    for st, c, lat, ti, to in calls:
        print(f"вызовов {st}: {c}, средняя латентность {lat / 1000:.0f} с, токены вход {ti:.0f} / выход {to:.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
