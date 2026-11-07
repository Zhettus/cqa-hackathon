"""Загрузка набора по group_id: API организаторов → fallback на data/{group_id}.json → БД."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ValidationError

from backend import db
from backend.config import get_settings

log = logging.getLogger(__name__)
GROUP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class TranscriptItem(BaseModel):
    timestamp: str
    speaker: Literal["client", "operator", "bot", "system"]
    text: str


class Record(BaseModel):
    conversation_id: str
    channel: str
    language: str | None = None
    transcript: list[TranscriptItem]
    audio_url: str | None = None


class DataSourceError(RuntimeError):
    pass


def parse_payload(payload: dict | list) -> list[dict]:
    """Принимает ответ API ({isSuccess, data: {records}}), {records} или список записей.
    Возвращает записи в исходном виде (поля не переписываются), после проверки схемы."""
    if isinstance(payload, dict):
        if payload.get("isSuccess") is False:
            raise DataSourceError(f"источник вернул ошибку: {payload.get('error')!r}")
        data = payload.get("data", payload)
        records = data.get("records") if isinstance(data, dict) else None
    else:
        records = payload
    if not isinstance(records, list):
        raise DataSourceError("в ответе нет data.records[]")
    try:
        for r in records:
            Record.model_validate(r)
    except ValidationError as e:
        raise DataSourceError(f"запись не соответствует формату: {e.errors()[0]}") from e
    ids = [r["conversation_id"] for r in records]
    if len(ids) != len(set(ids)):
        raise DataSourceError("дубли conversation_id в наборе")
    return records


def fetch_remote(group_id: str, url: str, timeout_s: float, client: httpx.Client | None = None) -> list[dict]:
    own = client is None
    client = client or httpx.Client(timeout=timeout_s)
    try:
        resp = client.get(url, params={"group_id": group_id})
        resp.raise_for_status()
        return parse_payload(resp.json())
    except (httpx.HTTPError, ValueError) as e:
        raise DataSourceError(f"API недоступно: {e}") from e
    finally:
        if own:
            client.close()


def load_local(group_id: str, data_dir: Path) -> list[dict]:
    path = data_dir / f"{group_id.lower()}.json"
    if not path.exists():
        path = data_dir / f"{group_id}.json"
    if not path.exists():
        raise DataSourceError(f"нет локального файла {path}")
    return parse_payload(json.loads(path.read_text(encoding="utf-8")))


def import_group(conn: sqlite3.Connection, group_id: str, prefer: Literal["remote", "local"] = "remote",
                 client: httpx.Client | None = None) -> dict:
    """Загружает набор и сохраняет в БД. remote: API, при ошибке — локальный файл."""
    if not GROUP_ID_RE.match(group_id):
        raise DataSourceError(f"некорректный group_id {group_id!r}")
    s = get_settings()
    errors = []
    if prefer == "remote" and s.data_api_url:
        try:
            records = fetch_remote(group_id, s.data_api_url, s.data_api_timeout_s, client)
            return db.save_dataset(conn, group_id, "remote", records)
        except DataSourceError as e:
            log.warning("group_id=%s: %s; пробую локальный файл", group_id, e)
            errors.append(str(e))
    elif prefer == "remote":
        errors.append("DATA_API_URL не задан")
    try:
        records = load_local(group_id, s.data_dir)
    except DataSourceError as e:
        raise DataSourceError("; ".join(errors + [str(e)])) from e
    result = db.save_dataset(conn, group_id, "local", records)
    result["warnings"] = errors
    return result


def import_upload(conn: sqlite3.Connection, payload: dict | list, group_id: str | None = None) -> dict:
    records = parse_payload(payload)
    gid = group_id or (payload.get("data", {}).get("dataset_group_id") if isinstance(payload, dict) else None) or "UPLOAD"
    return db.save_dataset(conn, gid, "upload", records)
