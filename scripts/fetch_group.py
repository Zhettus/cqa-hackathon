"""Получить набор по способу организаторов и сохранить исходный ответ API без изменений.

    python scripts/fetch_group.py <GROUP_ID> [--raw data/prod_raw.json] [--url ...]

- GET {DATA_API_URL}?group_id=<GROUP_ID> — тот же запрос, что делает сервер (backend/data_source.fetch_remote).
- Ответ сохраняется байт в байт в --raw (по умолчанию data/prod_raw.json) и копией в data/<group_id>.json —
  оттуда его подхватят `scripts/evaluate_group.py` и импорт с prefer=local.
- Печатает число записей, каналы и языки; код возврата 2, если ответ не разбирается как набор.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.config import get_settings  # noqa: E402
from backend.data_source import DataSourceError, parse_payload  # noqa: E402


def fetch(group_id: str, url: str, timeout_s: float, raw_path: Path, data_dir: Path,
          client: httpx.Client | None = None) -> list[dict]:
    own = client is None
    client = client or httpx.Client(timeout=timeout_s)
    try:
        resp = client.get(url, params={"group_id": group_id})
    finally:
        if own:
            client.close()
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(resp.content)                       # исходный ответ как есть, даже если это ошибка
    if resp.status_code >= 400:
        raise DataSourceError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        records = parse_payload(json.loads(resp.content.decode("utf-8")))
    except (UnicodeDecodeError, ValueError) as e:
        raise DataSourceError(f"ответ не JSON: {e}") from e
    local = data_dir / f"{group_id}.json"
    if local.resolve() != raw_path.resolve():
        shutil.copyfile(raw_path, local)
    return records


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("group_id")
    ap.add_argument("--raw", default=None, help="куда сохранить исходный ответ (по умолчанию data/prod_raw.json)")
    ap.add_argument("--url", default=None, help="адрес API (по умолчанию DATA_API_URL из .env)")
    args = ap.parse_args()
    s = get_settings()
    url = args.url or s.data_api_url
    if not url:
        print("DATA_API_URL не задан в .env (и нет --url)", file=sys.stderr)
        return 2
    raw = Path(args.raw) if args.raw else s.data_dir / "prod_raw.json"
    try:
        records = fetch(args.group_id, url, s.data_api_timeout_s, raw, s.data_dir)
    except (httpx.HTTPError, DataSourceError) as e:
        print(f"не удалось получить набор {args.group_id}: {e}", file=sys.stderr)
        return 2
    chan = Counter(r.get("channel") for r in records)
    lang = Counter(r.get("language") for r in records)
    print(f"{args.group_id}: {len(records)} записей → {raw} и {s.data_dir / (args.group_id + '.json')}")
    print(f"каналы: {dict(chan)}; языки: {dict(lang)}; реплик всего: {sum(len(r['transcript']) for r in records)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
