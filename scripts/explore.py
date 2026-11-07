"""Разведка датасета организаторов (data/dev.json по умолчанию).

Запуск: python scripts/explore.py [path/to/file.json]

Печатает: распределение по языкам и каналам, длины диалогов, роли спикеров,
все уникальные тексты speaker=system, примеры форматов timestamp, наличие audio_url,
а также несколько производных фактов, полезных для проектирования (паузы, первая реакция).
"""
from __future__ import annotations

import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

MMSS_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
HHMMSS_RE = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})$")


def parse_ts(ts: str) -> float | None:
    """Секунды от начала/эпохи для сортировки и пауз. None, если формат неизвестен."""
    m = MMSS_RE.match(ts)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = HHMMSS_RE.match(ts)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def ts_kind(ts: str) -> str:
    if MMSS_RE.match(ts):
        return "MM:SS"
    if HHMMSS_RE.match(ts):
        return "HH:MM:SS"
    try:
        datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return "ISO8601"
    except ValueError:
        return "UNKNOWN"


def fmt_hist(counter: Counter, total: int | None = None) -> str:
    lines = []
    for key, n in sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0]))):
        pct = f" ({n / total * 100:.0f}%)" if total else ""
        lines.append(f"  {key!s:<28} {n:>4}{pct}")
    return "\n".join(lines)


def describe(values: list[float], unit: str = "") -> str:
    if not values:
        return "  (нет данных)"
    vs = sorted(values)
    return (
        f"  min={vs[0]:.0f}{unit} p25={vs[len(vs)//4]:.0f}{unit} "
        f"median={statistics.median(vs):.0f}{unit} p75={vs[3*len(vs)//4]:.0f}{unit} "
        f"max={vs[-1]:.0f}{unit} mean={statistics.mean(vs):.1f}{unit}"
    )


def main(path: str) -> None:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    print(f"=== Файл: {path}")
    print(f"Верхний уровень: {list(raw.keys())}  isSuccess={raw.get('isSuccess')}  error={raw.get('error')!r}")
    data = raw["data"]
    records = data["records"]
    print(f"dataset_group_id={data.get('dataset_group_id')}  total_records={data.get('total_records')}  len(records)={len(records)}")
    print(f"Ключи записи: {sorted(set().union(*(r.keys() for r in records)))}")
    print(f"Ключи реплики: {sorted(set().union(*(t.keys() for r in records for t in r['transcript'])))}")

    # --- 1. Языки и каналы --------------------------------------------------------
    n = len(records)
    print("\n=== Языки")
    print(fmt_hist(Counter(r["language"] for r in records), n))
    print("\n=== Каналы")
    print(fmt_hist(Counter(r["channel"] for r in records), n))
    print("\n=== Язык × канал")
    print(fmt_hist(Counter(f"{r['language']} / {r['channel']}" for r in records), n))

    # --- conversation_id паттерны ---------------------------------------------------
    print("\n=== Префиксы conversation_id (до последнего '-')")
    print(fmt_hist(Counter(r["conversation_id"].rsplit("-", 1)[0] for r in records), n))
    ids = [r["conversation_id"] for r in records]
    dup = [k for k, v in Counter(ids).items() if v > 1]
    print(f"Дубликаты conversation_id: {dup or 'нет'}")

    # --- 2. Длины диалогов ----------------------------------------------------------
    print("\n=== Длины диалогов (реплик всего)")
    print(describe([len(r["transcript"]) for r in records]))
    for ch in sorted({r["channel"] for r in records}):
        print(f"  [{ch}]")
        print(describe([len(r["transcript"]) for r in records if r["channel"] == ch]))
    print("\n=== Реплик оператора на диалог")
    print(describe([sum(t["speaker"] == "operator" for t in r["transcript"]) for r in records]))
    no_op = [r["conversation_id"] for r in records if not any(t["speaker"] == "operator" for t in r["transcript"])]
    print(f"Диалоги БЕЗ реплик оператора: {no_op or 'нет'}")
    print("\n=== Длина текста реплики, символов")
    print(describe([len(t["text"]) for r in records for t in r["transcript"]]))
    print("\n=== Символов на диалог (оценка размера промпта)")
    print(describe([sum(len(t["text"]) for t in r["transcript"]) for r in records]))
    print("\n=== Длительность диалога (последний ts − первый ts), сек")
    for ch in sorted({r["channel"] for r in records}):
        durs = []
        for r in records:
            if r["channel"] != ch:
                continue
            secs = [parse_ts(t["timestamp"]) for t in r["transcript"]]
            secs = [s for s in secs if s is not None]
            if len(secs) >= 2:
                durs.append(max(secs) - min(secs))
        print(f"  [{ch}]")
        print(describe(durs, "s"))

    # --- 3. Роли спикеров ------------------------------------------------------------
    print("\n=== Роли спикеров (всего реплик)")
    sp = Counter(t["speaker"] for r in records for t in r["transcript"])
    print(fmt_hist(sp, sum(sp.values())))
    print("\n=== Роли спикеров по каналам")
    for ch in sorted({r["channel"] for r in records}):
        c = Counter(t["speaker"] for r in records if r["channel"] == ch for t in r["transcript"])
        print(f"  [{ch}] " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())))
    print("\n=== Первый спикер диалога")
    print(fmt_hist(Counter(r["transcript"][0]["speaker"] for r in records if r["transcript"]), n))
    print("\n=== Последний спикер диалога")
    print(fmt_hist(Counter(r["transcript"][-1]["speaker"] for r in records if r["transcript"]), n))

    # --- 4. ВСЕ уникальные тексты speaker=system -----------------------------------------
    print("\n=== ВСЕ уникальные тексты speaker=system")
    sys_texts: dict[str, list[str]] = defaultdict(list)
    for r in records:
        for t in r["transcript"]:
            if t["speaker"] == "system":
                sys_texts[t["text"]].append(r["conversation_id"])
    if not sys_texts:
        print("  (реплик system нет)")
    for text, convs in sorted(sys_texts.items(), key=lambda kv: -len(kv[1])):
        print(f"  [{len(convs):>3}×] {text!r}")
        print(f"         напр.: {', '.join(convs[:4])}")

    # --- Уникальные тексты бота (тоже контекст; показываем усечённо) --------------------
    print("\n=== Уникальные тексты speaker=bot (усечено до 110 символов)")
    bot_texts = Counter(t["text"] for r in records for t in r["transcript"] if t["speaker"] == "bot")
    print(f"  всего уникальных: {len(bot_texts)}")
    for text, cnt in bot_texts.most_common(25):
        print(f"  [{cnt:>3}×] {text[:110]!r}")

    # --- 5. Форматы timestamp ---------------------------------------------------------------
    print("\n=== Форматы timestamp по каналам")
    for ch in sorted({r["channel"] for r in records}):
        kinds = Counter(ts_kind(t["timestamp"]) for r in records if r["channel"] == ch for t in r["transcript"])
        print(f"  [{ch}] {dict(kinds)}")
        shown = 0
        for r in records:
            if r["channel"] == ch and shown < 2:
                ex = [t["timestamp"] for t in r["transcript"][:4]]
                print(f"      {r['conversation_id']}: {ex} ... {r['transcript'][-1]['timestamp']}")
                shown += 1
    print("\n=== Монотонность timestamp внутри диалога")
    non_mono = []
    for r in records:
        secs = [parse_ts(t["timestamp"]) for t in r["transcript"]]
        if any(s is None for s in secs):
            non_mono.append((r["conversation_id"], "неразобранный ts"))
            continue
        if any(b < a for a, b in zip(secs, secs[1:])):
            non_mono.append((r["conversation_id"], "убывает"))
    print(f"  нарушений: {non_mono or 'нет'}")

    # --- 6. audio_url ---------------------------------------------------------------------
    print("\n=== audio_url")
    au = Counter(
        "отсутствует ключ" if "audio_url" not in r else ("null" if r["audio_url"] is None else "есть строка")
        for r in records
    )
    print(fmt_hist(au, n))
    for r in records:
        if r.get("audio_url"):
            print(f"  пример: {r['conversation_id']} → {r['audio_url']}")
            break
    print("  по каналам: " + ", ".join(
        f"{ch}: {sum(1 for r in records if r['channel'] == ch and r.get('audio_url'))}/{sum(1 for r in records if r['channel'] == ch)} с audio"
        for ch in sorted({r['channel'] for r in records})
    ))

    # --- 7. Производные факты для правил (первая реакция, паузы) --------------------------
    print("\n=== Первая реакция оператора после подключения (сек, порог 180)")
    first_resp: list[float] = []
    over = []
    for r in records:
        tr = r["transcript"]
        secs = [parse_ts(t["timestamp"]) for t in tr]
        if any(s is None for s in secs):
            continue
        op_idx = next((i for i, t in enumerate(tr) if t["speaker"] == "operator"), None)
        if op_idx is None:
            continue
        # точка отсчёта: последняя system-реплика о подключении оператора, иначе последняя реплика клиента до оператора
        anchor = None
        for i in range(op_idx - 1, -1, -1):
            if tr[i]["speaker"] == "system":
                anchor = i
                break
        if anchor is None:
            for i in range(op_idx - 1, -1, -1):
                if tr[i]["speaker"] == "client":
                    anchor = i
                    break
        if anchor is None:
            continue
        gap = secs[op_idx] - secs[anchor]
        first_resp.append(gap)
        if gap > 180:
            over.append(f"{r['conversation_id']}({gap:.0f}s)")
    print(describe(first_resp, "s"))
    print(f"  > 180с: {len(over)} → {', '.join(over) or '—'}")

    print("\n=== Максимальная пауза между соседними репликами внутри диалога, сек")
    gaps_all = []
    for r in records:
        secs = [parse_ts(t["timestamp"]) for t in r["transcript"]]
        if any(s is None for s in secs) or len(secs) < 2:
            continue
        gaps_all.append(max(b - a for a, b in zip(secs, secs[1:])))
    print(describe(gaps_all, "s"))

    print("\n=== Пустые/пробельные тексты реплик")
    empty = [(r["conversation_id"], i) for r in records for i, t in enumerate(r["transcript"]) if not t["text"].strip()]
    print(f"  {empty or 'нет'}")

    print("\n=== Смешение языков: диалоги, где язык записи ≠ преобладающему алфавиту реплик клиента")
    def script_of(s: str) -> str:
        cyr = sum("а" <= ch.lower() <= "я" or ch.lower() in "ёіңғүұқөһ" for ch in s)
        lat = sum("a" <= ch.lower() <= "z" for ch in s)
        kz_specific = sum(ch.lower() in "іңғүұқөһ" for ch in s)
        if kz_specific:
            return "KZ"
        if cyr >= lat:
            return "RU" if cyr else "?"
        return "EN"
    mism = []
    for r in records:
        texts = " ".join(t["text"] for t in r["transcript"] if t["speaker"] == "client")
        guess = script_of(texts)
        if guess != r["language"]:
            mism.append(f"{r['conversation_id']}: label={r['language']} guess={guess}")
    print("  " + ("\n  ".join(mism) if mism else "несоответствий нет"))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/dev.json")
