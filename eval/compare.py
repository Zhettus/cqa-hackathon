"""Сравнение двух прогонов eval/run.py по их JSON-отчётам.

  python -m eval.compare eval/reports/A.json eval/reports/B.json [--md out.md]

Показывает дельту совпадения по каждому критерию, MAE итога, случаи edge и
какие ответы стали верными/неверными (диалог, критерий).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


def load(path: str) -> dict:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = {}
    for r in d["results"]:
        for c in r["criteria"]:
            rows[(r["conversation_id"], c["criterion_id"])] = c
    totals = {r["conversation_id"]: (r["total"], r["gold_total"]) for r in d["results"]}
    edge = {e["conversation_id"]: e["passed"] for e in d.get("edge", [])}
    return {"meta": d["meta"], "rows": rows, "totals": totals, "edge": edge}


def mae(totals: dict) -> float | None:
    errs = [abs(a - b) for a, b in totals.values() if a is not None and b is not None]
    return sum(errs) / len(errs) if errs else None


def compare(a: dict, b: dict) -> dict:
    keys = sorted(set(a["rows"]) & set(b["rows"]))
    per = defaultdict(lambda: {"n": 0, "a": 0, "b": 0, "fixed": [], "broken": []})
    for k in keys:
        cid, crit = k
        p = per[crit]
        p["n"] += 1
        ma, mb = a["rows"][k]["match"], b["rows"][k]["match"]
        p["a"] += ma
        p["b"] += mb
        if mb and not ma:
            p["fixed"].append(cid)
        if ma and not mb:
            p["broken"].append(cid)
    n = sum(p["n"] for p in per.values())
    return {"per": dict(sorted(per.items())), "n": n, "a": sum(p["a"] for p in per.values()),
            "b": sum(p["b"] for p in per.values()), "mae_a": mae(a["totals"]), "mae_b": mae(b["totals"]),
            "edge": {k: (a["edge"].get(k), b["edge"].get(k)) for k in sorted(set(a["edge"]) | set(b["edge"]))}}


def render(a: dict, b: dict, c: dict) -> str:
    f = lambda x, n: f"{x}/{n} = {x / n:.1%}" if n else "—"  # noqa: E731
    L = [f"Было: промпт {a['meta']['prompt_version']} ({a['meta']['started']}); "
         f"стало: промпт {b['meta']['prompt_version']} ({b['meta']['started']})", "",
         "| Критерий | Было | Стало | Δ | Стали верными | Стали неверными |", "|---|---|---|---|---|---|"]
    for crit, p in c["per"].items():
        d = p["b"] - p["a"]
        L.append(f"| {crit} | {f(p['a'], p['n'])} | {f(p['b'], p['n'])} | {d:+d} | "
                 f"{', '.join(p['fixed']) or '—'} | {', '.join(p['broken']) or '—'} |")
    L.append(f"| **всего** | {f(c['a'], c['n'])} | {f(c['b'], c['n'])} | {c['b'] - c['a']:+d} | | |")
    if c["mae_a"] is not None and c["mae_b"] is not None:
        L += ["", f"MAE итога: {c['mae_a']:.2f} → {c['mae_b']:.2f}"]
    if c["edge"]:
        pa = sum(1 for x, _ in c["edge"].values() if x)
        pb = sum(1 for _, y in c["edge"].values() if y)
        changed = [f"{k}: {'✅' if x else '❌'}→{'✅' if y else '❌'}" for k, (x, y) in c["edge"].items() if x != y]
        L += [f"Edge пройдено: {pa} → {pb}" + (f" ({'; '.join(changed)})" if changed else "")]
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--md")
    args = ap.parse_args()
    a, b = load(args.before), load(args.after)
    out = render(a, b, compare(a, b))
    print(out)
    if args.md:
        Path(args.md).write_text(out, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
