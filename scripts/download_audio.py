"""Скачивает audio_url из датасета в data/audio/{conversation_id}.<ext>.

Запуск: python scripts/download_audio.py [data/dev.json] [--out data/audio] [--force]

Расширение — по Content-Type ответа. audio_url организаторов отдаёт HTML-страницу
плеера; из неё берётся <source src> / <audio src> (подписанная ссылка живёт ~1 час,
поэтому она не сохраняется, а извлекается заново при каждом запуске). Уже скачанные файлы пропускаются (без --force).
Если вместо аудио пришёл JSON/HTML/текст, файл не сохраняется как аудио:
тело кладётся в {id}.response.txt для разбора, запись помечается ошибкой.
Только стандартная библиотека.
"""
from __future__ import annotations

import argparse
import html
import json
import mimetypes
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

EXT_BY_TYPE = {
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav",
    "audio/wave": ".wav", "audio/vnd.wave": ".wav", "audio/ogg": ".ogg", "audio/opus": ".opus",
    "audio/webm": ".webm", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".aac",
    "audio/flac": ".flac", "audio/x-flac": ".flac", "video/mp4": ".mp4",
}
NON_AUDIO_PREFIXES = ("application/json", "text/", "application/xml", "application/problem+json")
TIMEOUT_S = 120
SRC_RE = re.compile(r"<(?:source|audio)\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.I)


def _get(url: str) -> tuple[str, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "cqa-audio-downloader/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return resp.headers.get("Content-Type", "application/octet-stream"), resp.read()


def ext_for(content_type: str) -> str:
    ct = content_type.split(";")[0].strip().lower()
    return EXT_BY_TYPE.get(ct) or mimetypes.guess_extension(ct) or ".bin"


def fetch(conv_id: str, url: str, out: Path, force: bool) -> str:
    existing = [p for p in out.glob(f"{conv_id}.*") if not p.name.endswith((".part", ".response.txt"))]
    if existing and not force:
        return f"SKIP  {conv_id}: уже есть {existing[0].name}"
    try:
        ctype, body = _get(url)
        if ctype.lower().startswith("text/html"):
            m = SRC_RE.search(body.decode("utf-8", "replace"))
            if m:
                ctype, body = _get(html.unescape(m.group(1)))
    except urllib.error.HTTPError as e:
        return f"FAIL  {conv_id}: HTTP {e.code} {e.reason}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return f"FAIL  {conv_id}: сеть недоступна ({e})"
    if ctype.split(";")[0].strip().lower().startswith(NON_AUDIO_PREFIXES):
        (out / f"{conv_id}.response.txt").write_bytes(body)
        return f"FAIL  {conv_id}: пришло не аудио ({ctype}, {len(body)} байт) → {conv_id}.response.txt"
    target = out / f"{conv_id}{ext_for(ctype)}"
    tmp = target.with_name(target.name + ".part")
    tmp.write_bytes(body)
    tmp.replace(target)
    return f"OK    {conv_id}: {target.name} ({ctype}, {len(body) / 1024:.0f} КБ)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset", nargs="?", default="data/dev.json")
    ap.add_argument("--out", default="data/audio")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    records = json.loads(Path(args.dataset).read_text(encoding="utf-8"))["data"]["records"]
    todo = [(r["conversation_id"], r["audio_url"]) for r in records if r.get("audio_url")]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    print(f"Записей с audio_url: {len(todo)}")
    results = [fetch(cid, url, out, args.force) for cid, url in todo]
    print("\n".join(results))
    failed = sum(r.startswith("FAIL") for r in results)
    print(f"Итого: {len(results) - failed} успешно, {failed} с ошибкой")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
