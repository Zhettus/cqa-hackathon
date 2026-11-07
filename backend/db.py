"""SQLite-хранилище.

Неизменяемость (инварианты 6, 7) обеспечена триггерами: версии чек-листов,
оценки, результаты по критериям, логи LLM и решения по апелляциям нельзя
изменить или удалить. Статус апелляции не хранится — он выводится из наличия решения.
Исходный транскрипт хранится как получен (JSON-строка), не модифицируется.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.checklist import load_default_checklist, validate_checklist

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    group_id      TEXT PRIMARY KEY,
    source        TEXT NOT NULL,            -- remote | local | upload
    total_records INTEGER NOT NULL,
    imported_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    group_id        TEXT NOT NULL,
    channel         TEXT NOT NULL,
    language        TEXT,
    audio_url       TEXT,
    transcript_json TEXT NOT NULL,          -- исходник без изменений
    imported_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_conv_group ON conversations(group_id);

CREATE TABLE IF NOT EXISTS checklists (
    id         TEXT NOT NULL,
    version    INTEGER NOT NULL,
    json       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    author     TEXT,
    comment    TEXT,
    PRIMARY KEY (id, version)
);

CREATE TABLE IF NOT EXISTS evaluations (
    id                TEXT PRIMARY KEY,
    conversation_id   TEXT NOT NULL REFERENCES conversations(conversation_id),
    checklist_id      TEXT NOT NULL,
    checklist_version INTEGER NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('completed','not_evaluable','failed')),
    status_reason     TEXT,
    total             REAL,
    sections_json     TEXT NOT NULL DEFAULT '[]',
    flags_json        TEXT NOT NULL DEFAULT '[]',
    facts_json        TEXT NOT NULL DEFAULT '{}',
    model_provider    TEXT,
    model_name        TEXT,
    batch_id          TEXT,
    duration_ms       INTEGER,
    created_at        TEXT NOT NULL,
    FOREIGN KEY (checklist_id, checklist_version) REFERENCES checklists(id, version)
);
CREATE INDEX IF NOT EXISTS ix_eval_conv ON evaluations(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS ix_eval_batch ON evaluations(batch_id);

CREATE TABLE IF NOT EXISTS criterion_results (
    evaluation_id TEXT NOT NULL REFERENCES evaluations(id),
    criterion_id  TEXT NOT NULL,
    section       TEXT NOT NULL,
    type          TEXT NOT NULL,
    weight        REAL NOT NULL,
    answer_json   TEXT NOT NULL,            -- "yes" | "no" | "na" | число
    applicable    INTEGER NOT NULL,
    points        REAL,
    explanation   TEXT,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (evaluation_id, criterion_id)
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id                     TEXT PRIMARY KEY,
    evaluation_id          TEXT,
    conversation_id        TEXT,
    batch_id               TEXT,
    attempt                INTEGER NOT NULL DEFAULT 1,
    provider               TEXT NOT NULL,
    model                  TEXT NOT NULL,
    started_at             TEXT NOT NULL,
    latency_ms             INTEGER,
    tokens_in              INTEGER,
    tokens_out             INTEGER,
    cost_usd               REAL,
    status                 TEXT NOT NULL CHECK (status IN ('ok','invalid_response','provider_error')),
    validation_errors_json TEXT NOT NULL DEFAULT '[]',
    request_json           TEXT NOT NULL,
    raw_response           TEXT
);
CREATE INDEX IF NOT EXISTS ix_llm_eval ON llm_calls(evaluation_id);

CREATE TABLE IF NOT EXISTS appeals (
    id            TEXT PRIMARY KEY,
    evaluation_id TEXT NOT NULL REFERENCES evaluations(id),
    criterion_id  TEXT NOT NULL,
    comment       TEXT NOT NULL,
    author        TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_appeal_eval ON appeals(evaluation_id);

CREATE TABLE IF NOT EXISTS appeal_decisions (
    id              TEXT PRIMARY KEY,
    appeal_id       TEXT NOT NULL UNIQUE REFERENCES appeals(id),
    decision        TEXT NOT NULL CHECK (decision IN ('confirm','change')),
    new_answer_json TEXT,
    reason          TEXT,
    reviewer        TEXT,
    decided_at      TEXT NOT NULL
);
"""

IMMUTABLE_TABLES = ["checklists", "evaluations", "criterion_results", "llm_calls", "appeals", "appeal_decisions"]


def _triggers() -> str:
    out = []
    for t in IMMUTABLE_TABLES:
        for op in ("UPDATE", "DELETE"):
            out.append(
                f"CREATE TRIGGER IF NOT EXISTS trg_{t}_no_{op.lower()} BEFORE {op} ON {t} "
                f"BEGIN SELECT RAISE(ABORT, '{t}: запись неизменяема'); END;"
            )
    return "\n".join(out)


class NotFound(LookupError):
    pass


class Conflict(RuntimeError):
    pass


class ValidationFailed(ValueError):
    def __init__(self, errors: list[dict]):
        super().__init__("; ".join(f"{e['path']}: {e['message']}" for e in errors))
        self.errors = errors


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _j(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False)


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL") if str(path) != ":memory:" else None
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Создаёт схему; при первом старте кладёт чек-лист из SPEC как версию 1."""
    conn.executescript(SCHEMA + _triggers())
    default = load_default_checklist()
    exists = conn.execute("SELECT 1 FROM checklists WHERE id = ?", (default["id"],)).fetchone()
    if not exists:
        insert_checklist_version(conn, default, author="system", comment="Исходный чек-лист из docs/SPEC.md",
                                 version=int(default.get("version", 1)))
    conn.commit()


# --- наборы и диалоги ------------------------------------------------------------------
def save_dataset(conn: sqlite3.Connection, group_id: str, source: str, records: list[dict]) -> dict:
    ts = now()
    with conn:
        conn.execute(
            "INSERT INTO datasets(group_id, source, total_records, imported_at) VALUES (?,?,?,?) "
            "ON CONFLICT(group_id) DO UPDATE SET source=excluded.source, "
            "total_records=excluded.total_records, imported_at=excluded.imported_at",
            (group_id, source, len(records), ts),
        )
        for r in records:
            conn.execute(
                "INSERT INTO conversations(conversation_id, group_id, channel, language, audio_url, "
                "transcript_json, imported_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(conversation_id) DO UPDATE SET group_id=excluded.group_id, "
                "channel=excluded.channel, language=excluded.language, audio_url=excluded.audio_url, "
                "transcript_json=excluded.transcript_json, imported_at=excluded.imported_at",
                (r["conversation_id"], group_id, r["channel"], r.get("language"), r.get("audio_url"),
                 _j(r["transcript"]), ts),
            )
    return {"group_id": group_id, "source": source, "total_records": len(records),
            "imported": len(records), "imported_at": ts}


def get_dataset(conn: sqlite3.Connection, group_id: str) -> dict:
    row = conn.execute("SELECT * FROM datasets WHERE group_id = ?", (group_id,)).fetchone()
    if not row:
        raise NotFound(f"набор {group_id} не найден")
    return dict(row)


def list_datasets(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM datasets ORDER BY group_id")]


def _conv(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["transcript"] = json.loads(d.pop("transcript_json"))
    return d


def get_conversation(conn: sqlite3.Connection, conversation_id: str) -> dict:
    row = conn.execute("SELECT * FROM conversations WHERE conversation_id = ?", (conversation_id,)).fetchone()
    if not row:
        raise NotFound(f"диалог {conversation_id} не найден")
    return _conv(row)


def list_conversations(conn: sqlite3.Connection, group_id: str | None = None) -> list[dict]:
    if group_id:
        rows = conn.execute("SELECT * FROM conversations WHERE group_id = ? ORDER BY conversation_id", (group_id,))
    else:
        rows = conn.execute("SELECT * FROM conversations ORDER BY group_id, conversation_id")
    return [_conv(r) for r in rows]


# --- чек-листы ------------------------------------------------------------------------
def insert_checklist_version(conn: sqlite3.Connection, checklist: dict, *, author: str | None = None,
                             comment: str | None = None, base_version: int | None = None,
                             version: int | None = None) -> dict:
    """Новая версия. base_version ≠ последней → Conflict. Невалидный чек-лист → ValidationFailed."""
    errors = validate_checklist(checklist)
    if errors:
        raise ValidationFailed(errors)
    cid = checklist["id"]
    latest = conn.execute("SELECT MAX(version) FROM checklists WHERE id = ?", (cid,)).fetchone()[0]
    if base_version is not None and base_version != latest:
        raise Conflict(f"чек-лист {cid}: базовая версия {base_version}, последняя {latest}")
    if version is None:
        version = (latest or 0) + 1
    body = {k: v for k, v in checklist.items() if k not in ("version", "base_version", "author", "comment")}
    body["version"] = version
    ts = now()
    with conn:
        conn.execute("INSERT INTO checklists(id, version, json, created_at, author, comment) VALUES (?,?,?,?,?,?)",
                     (cid, version, _j(body), ts, author, comment))
    return get_checklist(conn, cid, version)


def get_checklist(conn: sqlite3.Connection, checklist_id: str, version: int | None = None) -> dict:
    if version is None:
        row = conn.execute("SELECT * FROM checklists WHERE id = ? ORDER BY version DESC LIMIT 1",
                           (checklist_id,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM checklists WHERE id = ? AND version = ?",
                           (checklist_id, version)).fetchone()
    if not row:
        raise NotFound(f"чек-лист {checklist_id} v{version or 'latest'} не найден")
    body = json.loads(row["json"])
    latest = conn.execute("SELECT MAX(version) FROM checklists WHERE id = ?", (checklist_id,)).fetchone()[0]
    return {**body, "created_at": row["created_at"], "author": row["author"], "comment": row["comment"],
            "is_latest": row["version"] == latest}


def list_checklist_versions(conn: sqlite3.Connection, checklist_id: str) -> list[dict]:
    rows = conn.execute("SELECT version, json, created_at, author, comment FROM checklists "
                        "WHERE id = ? ORDER BY version", (checklist_id,)).fetchall()
    if not rows:
        raise NotFound(f"чек-лист {checklist_id} не найден")
    return [{"version": r["version"], "created_at": r["created_at"], "author": r["author"],
             "comment": r["comment"], "criteria_count": len(json.loads(r["json"])["criteria"])} for r in rows]


def list_checklists(conn: sqlite3.Connection) -> list[dict]:
    ids = [r[0] for r in conn.execute("SELECT DISTINCT id FROM checklists ORDER BY id")]
    out = []
    for cid in ids:
        cl = get_checklist(conn, cid)
        out.append({"id": cid, "version": cl["version"], "name": cl["name"],
                    "created_at": cl["created_at"], "criteria_count": len(cl["criteria"])})
    return out


# --- оценки -----------------------------------------------------------------------------
def insert_evaluation(conn: sqlite3.Connection, ev: dict, criteria: list[dict]) -> str:
    """Сохраняет оценку и результаты по критериям одной транзакцией. Возвращает id."""
    eid = ev.get("id") or new_id("ev")
    with conn:
        conn.execute(
            "INSERT INTO evaluations(id, conversation_id, checklist_id, checklist_version, status, status_reason, "
            "total, sections_json, flags_json, facts_json, model_provider, model_name, batch_id, duration_ms, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, ev["conversation_id"], ev["checklist_id"], ev["checklist_version"], ev["status"],
             ev.get("status_reason"), ev.get("total"), _j(ev.get("sections", [])), _j(ev.get("flags", [])),
             _j(ev.get("facts", {})), ev.get("model_provider"), ev.get("model_name"), ev.get("batch_id"),
             ev.get("duration_ms"), ev.get("created_at") or now()),
        )
        for c in criteria:
            conn.execute(
                "INSERT INTO criterion_results(evaluation_id, criterion_id, section, type, weight, answer_json, "
                "applicable, points, explanation, evidence_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (eid, c["criterion_id"], c["section"], c["type"], c["weight"], _j(c["answer"]),
                 int(bool(c["applicable"])), c.get("points"), c.get("explanation"), _j(c.get("evidence", {}))),
            )
    return eid


def get_evaluation(conn: sqlite3.Connection, evaluation_id: str) -> dict:
    row = conn.execute("SELECT * FROM evaluations WHERE id = ?", (evaluation_id,)).fetchone()
    if not row:
        raise NotFound(f"оценка {evaluation_id} не найдена")
    d = dict(row)
    for k in ("sections", "flags", "facts"):
        d[k] = json.loads(d.pop(f"{k}_json"))
    d["checklist"] = {"id": d.pop("checklist_id"), "version": d.pop("checklist_version")}
    d["model"] = {"provider": d.pop("model_provider"), "name": d.pop("model_name")}
    crit = conn.execute("SELECT * FROM criterion_results WHERE evaluation_id = ? ORDER BY rowid",
                        (evaluation_id,)).fetchall()
    d["criteria"] = [{"criterion_id": c["criterion_id"], "section": c["section"], "type": c["type"],
                      "weight": c["weight"], "answer": json.loads(c["answer_json"]),
                      "applicable": bool(c["applicable"]), "points": c["points"],
                      "explanation": c["explanation"], "evidence": json.loads(c["evidence_json"])} for c in crit]
    d["llm_call_ids"] = [r[0] for r in conn.execute(
        "SELECT id FROM llm_calls WHERE evaluation_id = ? ORDER BY started_at", (evaluation_id,))]
    return d


# --- логи LLM ---------------------------------------------------------------------------
def insert_llm_call(conn: sqlite3.Connection, call: dict) -> str:
    cid = call.get("id") or new_id("llm")
    with conn:
        conn.execute(
            "INSERT INTO llm_calls(id, evaluation_id, conversation_id, batch_id, attempt, provider, model, "
            "started_at, latency_ms, tokens_in, tokens_out, cost_usd, status, validation_errors_json, "
            "request_json, raw_response) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, call.get("evaluation_id"), call.get("conversation_id"), call.get("batch_id"),
             call.get("attempt", 1), call["provider"], call["model"], call.get("started_at") or now(),
             call.get("latency_ms"), call.get("tokens_in"), call.get("tokens_out"), call.get("cost_usd"),
             call["status"], _j(call.get("validation_errors", [])), _j(call["request"]),
             call.get("raw_response")),
        )
    return cid


def get_llm_call(conn: sqlite3.Connection, call_id: str) -> dict:
    row = conn.execute("SELECT * FROM llm_calls WHERE id = ?", (call_id,)).fetchone()
    if not row:
        raise NotFound(f"вызов {call_id} не найден")
    d = dict(row)
    d["validation_errors"] = json.loads(d.pop("validation_errors_json"))
    d["request"] = json.loads(d.pop("request_json"))
    return d


# --- апелляции --------------------------------------------------------------------------
def insert_appeal(conn: sqlite3.Connection, evaluation_id: str, criterion_id: str, comment: str,
                  author: str | None = None) -> dict:
    ev = get_evaluation(conn, evaluation_id)
    if criterion_id not in {c["criterion_id"] for c in ev["criteria"]}:
        raise NotFound(f"в оценке {evaluation_id} нет критерия {criterion_id}")
    if not comment or not comment.strip():
        raise ValidationFailed([{"path": "comment", "message": "нужен комментарий"}])
    open_ = conn.execute(
        "SELECT a.id FROM appeals a LEFT JOIN appeal_decisions d ON d.appeal_id = a.id "
        "WHERE a.evaluation_id = ? AND a.criterion_id = ? AND d.id IS NULL", (evaluation_id, criterion_id)).fetchone()
    if open_:
        raise Conflict(f"по критерию {criterion_id} уже есть открытая апелляция {open_[0]}")
    aid = new_id("ap")
    with conn:
        conn.execute("INSERT INTO appeals(id, evaluation_id, criterion_id, comment, author, created_at) "
                     "VALUES (?,?,?,?,?,?)", (aid, evaluation_id, criterion_id, comment.strip(), author, now()))
    return get_appeal(conn, aid)


def insert_decision(conn: sqlite3.Connection, appeal_id: str, decision: str, *, new_answer: Any = None,
                    reason: str | None = None, reviewer: str | None = None) -> dict:
    """Решение по апелляции. Проверка new_answer по критерию — в вызывающем коде (scoring.validate_answer)."""
    appeal = get_appeal(conn, appeal_id)
    if appeal["decision"] is not None:
        raise Conflict(f"по апелляции {appeal_id} решение уже принято")
    if decision not in ("confirm", "change"):
        raise ValidationFailed([{"path": "decision", "message": "confirm или change"}])
    if decision == "change" and (new_answer is None or not (reason and reason.strip())):
        raise ValidationFailed([{"path": "new_answer/reason", "message": "для change нужны новый ответ и причина"}])
    with conn:
        conn.execute("INSERT INTO appeal_decisions(id, appeal_id, decision, new_answer_json, reason, reviewer, "
                     "decided_at) VALUES (?,?,?,?,?,?,?)",
                     (new_id("dc"), appeal_id, decision, None if decision == "confirm" else _j(new_answer),
                      reason, reviewer, now()))
    return get_appeal(conn, appeal_id)


def get_appeal(conn: sqlite3.Connection, appeal_id: str) -> dict:
    row = conn.execute("SELECT * FROM appeals WHERE id = ?", (appeal_id,)).fetchone()
    if not row:
        raise NotFound(f"апелляция {appeal_id} не найдена")
    return _appeal(conn, row)


def list_appeals(conn: sqlite3.Connection, evaluation_id: str | None = None) -> list[dict]:
    if evaluation_id:
        rows = conn.execute("SELECT * FROM appeals WHERE evaluation_id = ? ORDER BY created_at", (evaluation_id,))
    else:
        rows = conn.execute("SELECT * FROM appeals ORDER BY created_at")
    return [_appeal(conn, r) for r in rows.fetchall()]


def _appeal(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    d = dict(row)
    dec = conn.execute("SELECT * FROM appeal_decisions WHERE appeal_id = ?", (d["id"],)).fetchone()
    d["status"] = "decided" if dec else "open"
    d["decision"] = None if not dec else {
        "decision": dec["decision"],
        "new_answer": None if dec["new_answer_json"] is None else json.loads(dec["new_answer_json"]),
        "reason": dec["reason"], "reviewer": dec["reviewer"], "decided_at": dec["decided_at"],
    }
    return d
