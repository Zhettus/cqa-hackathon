"""Репетиция этапа 7: замена критерия через API и повторное применение пакетом.

Запуск (сервер должен работать):
    python scripts/rehearsal.py --base http://localhost:8000 --group DEV --limit 10 [--only-chats]

Шаги (всё через REST, как сделает UI):
  1. GET  /api/checklists/chat-quality             — текущая версия v1.
  2. POST /api/checklists/chat-quality/versions    — v2: заменён один критерий (формулировка и шкала c06),
     base_version = v1 (проверка конфликта версий).
  3. GET  /api/checklists/chat-quality/versions/1/diff/2 — что изменилось.
  4. POST /api/batches {group_id или conversation_ids, checklist_version: 2} → опрос прогресса до завершения.
  5. GET  /api/results?checklist_version=1 и =2 — сравнение итогов по тем же диалогам, CSV v2.
Печатает время пакета, стоимость, число completed/not_evaluable/failed и таблицу итогов v1 vs v2.
Оценки v1 не меняются (инвариант 6): v2 — новые записи с другой версией чек-листа.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import time

import httpx


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--group", default="DEV")
    ap.add_argument("--limit", type=int, default=10, help="сколько диалогов набора взять в пакет")
    ap.add_argument("--only-chats", action="store_true", help="исключить звонки из пакета")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--poll", type=float, default=2.0)
    args = ap.parse_args()
    c = httpx.Client(base_url=args.base, timeout=120)

    def must(r: httpx.Response, code: int) -> dict:
        if r.status_code != code:
            print(f"ОШИБКА {r.request.method} {r.request.url}: {r.status_code} {r.text[:300]}", file=sys.stderr)
            sys.exit(2)
        return r.json()

    print(f"[1] чек-лист: ", end="")
    v1 = must(c.get("/api/checklists/chat-quality"), 200)
    print(f"{v1['id']} v{v1['version']} ({len(v1['criteria'])} критериев)")

    # [2] новая версия: заменяем c06 — другая формулировка и трёхступенчатая шкала (0/5/10)
    body = {k: copy.deepcopy(v1[k]) for k in ("name", "sections", "criteria", "settings")}
    c06 = next(x for x in body["criteria"] if x["id"] == "c06")
    c06.update({
        "title": "Ведение диалога (заменённый критерий)",
        "description": "Оцените, насколько оператор вёл диалог ясно и последовательно, предупреждал об ожидании.",
        "scale": [{"value": 10, "description": "Ясно, последовательно, ожидание предупреждено."},
                  {"value": 5, "description": "Есть заметный сбой (не по теме, противоречие, ожидание без предупреждения)."},
                  {"value": 0, "description": "Ход диалога непонятен."}],
        "guards": [{"id": "c06_timing", "when": {"any": [
            {"fact": "first_response_s", "op": ">", "value_from": "settings.first_response_threshold_s"},
            {"fact": "max_unwarned_gap_s", "op": ">", "value_from": "settings.pause_threshold_s"}]},
            "max_answer": 5, "note": "Первая реакция или пауза без предупреждения дольше порога — не выше 5"}],
    })
    print("[2] конфликт версий: ", end="")
    r = c.post("/api/checklists/chat-quality/versions", json={**body, "base_version": v1["version"] + 7})
    print("409 получен" if r.status_code == 409 else f"ожидался 409, получен {r.status_code}")
    latest = must(c.get("/api/checklists/chat-quality"), 200)["version"]
    v2 = must(c.post("/api/checklists/chat-quality/versions",
                     json={**body, "base_version": latest, "author": "rehearsal", "comment": "Заменён c06"}), 201)
    print(f"    создана версия v{v2['version']}")

    print("[3] diff v1→v2: ", end="")
    diff = must(c.get(f"/api/checklists/chat-quality/versions/{v1['version']}/diff/{v2['version']}"), 200)
    print(json.dumps({k: diff[k] for k in ("added", "removed", "changed")}, ensure_ascii=False))

    convs = must(c.get("/api/conversations", params={"group_id": args.group, "limit": 500}), 200)["items"]
    if args.only_chats:
        convs = [x for x in convs if x["channel"] == "agent chat"]
    ids = [x["conversation_id"] for x in convs[: args.limit]]
    print(f"[4] пакет: {len(ids)} диалогов, чек-лист v{v2['version']}, concurrency {args.concurrency}")
    t0 = time.time()
    b = must(c.post("/api/batches", json={"conversation_ids": ids, "checklist_id": "chat-quality",
                                            "checklist_version": v2["version"], "concurrency": args.concurrency}), 202)
    bid = b["batch_id"]
    while True:
        st = must(c.get(f"/api/batches/{bid}"), 200)
        p = st["progress"]
        print(f"    {st['status']}: {p['done']}/{p['total']} ({p['percent']}%)  completed={p['completed']} "
              f"not_evaluable={p['not_evaluable']} failed={p['failed']}  ${st['cost']['usd']:.2f}", flush=True)
        if st["status"] in ("completed", "cancelled", "failed"):
            break
        time.sleep(args.poll)
    elapsed = time.time() - t0
    print(f"    время пакета: {elapsed:.0f} с ({elapsed / max(1, len(ids)):.1f} с на диалог), стоимость ${st['cost']['usd']:.2f}")
    for it in st["items"]:
        if it["status"] == "failed":
            print(f"    FAILED {it['conversation_id']}: {it['error']}")

    print("[5] результаты v1 vs v2:")
    res1 = {i["conversation_id"]: i for i in must(c.get("/api/results", params={"checklist_version": v1["version"]}), 200)["items"]}
    res2 = {i["conversation_id"]: i for i in must(c.get("/api/results", params={"checklist_version": v2["version"]}), 200)["items"]}
    print(f"    {'диалог':<14}{'v1 итог':>9}{'v1 c06':>8}{'v2 итог':>9}{'v2 c06':>8}")
    for cid in ids:
        a, b2 = res1.get(cid), res2.get(cid)
        f = lambda x, k: "—" if not x else (x["total"] if k == "total" else x["answers"].get("c06", "—"))  # noqa: E731
        print(f"    {cid:<14}{str(f(a, 'total')):>9}{str(f(a, 'c06')):>8}{str(f(b2, 'total')):>9}{str(f(b2, 'c06')):>8}")
    csv = c.get("/api/results", params={"checklist_version": v2["version"], "format": "csv"})
    print(f"    CSV v2: {csv.status_code}, {len(csv.text.splitlines()) - 1} строк")
    return 0


if __name__ == "__main__":
    sys.exit(main())
