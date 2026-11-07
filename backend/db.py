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
    prompt_version    TEXT,
    votes             INTEGER,
    effort            TEXT,
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
    meta_json     TEXT NOT NULL DEFAULT '{}',   -- confidence, голоса, guards, подсказки, violation_type
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

-- Журнал апелляции (append-only): opened → in_review → decided. Статус выводится из журнала и решения.
CREATE TABLE IF NOT EXISTS appeal_events (
    id         TEXT PRIMARY KEY,
    appeal_id  TEXT NOT NULL REFERENCES appeals(id),
    event      TEXT NOT NULL CHECK (event IN ('opened','in_review','decided')),
    actor      TEXT,
    note       TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_appeal_events ON appeal_events(appeal_id);

-- Пакетная оценка: изменяемые таблицы прогресса (сами оценки — в evaluations, неизменяемы).
CREATE TABLE IF NOT EXISTS batches (
    id                TEXT PRIMARY KEY,
    group_id          TEXT,
    checklist_id      TEXT NOT NULL,
    checklist_version INTEGER NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('queued','running','completed','cancelled','failed')),
    concurrency       INTEGER NOT NULL DEFAULT 4,
    error             TEXT,
    cancel_requested  INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    finished_at       TEXT
);
CREATE TABLE IF NOT EXISTS batch_items (
    batch_id        TEXT NOT NULL REFERENCES batches(id),
    conversation_id TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('queued','running','completed','not_evaluable','failed','cancelled')),
    evaluation_id   TEXT,
    total           REAL,
    error           TEXT,
    PRIMARY KEY (batch_id, conversation_id)
);
"""

IMMUTABLE_TABLES = ["checklists", "evaluations", "criterion_results", "llm_calls", "appeals", "appeal_decisions",
                    "appeal_events"]


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


MIGRATIONS = {"evaluations": {"prompt_version": "TEXT", "votes": "INTEGER", "effort": "TEXT"},
              "criterion_results": {"meta_json": "TEXT NOT NULL DEFAULT '{}'"}}


def _migrate(conn: sqlite3.Connection) -> None:
    """Добавляет колонки, появившиеся после создания БД (только ADD COLUMN, данные не меняются)."""
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db(conn: sqlite3.Connection) -> None:
    """Создаёт схему; при первом старте кладёт чек-лист из SPEC как версию 1."""
    conn.executescript(SCHEMA + _triggers())
    _migrate(conn)
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
            "total, sections_json, flags_json, facts_json, model_provider, model_name, prompt_version, votes, "
            "effort, batch_id, duration_ms, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, ev["conversation_id"], ev["checklist_id"], ev["checklist_version"], ev["status"],
             ev.get("status_reason"), ev.get("total"), _j(ev.get("sections", [])), _j(ev.get("flags", [])),
             _j(ev.get("facts", {})), ev.get("model_provider"), ev.get("model_name"), ev.get("prompt_version"),
             ev.get("votes"), ev.get("effort"), ev.get("batch_id"),
             ev.get("duration_ms"), ev.get("created_at") or now()),
        )
        for c in criteria:
            conn.execute(
                "INSERT INTO criterion_results(evaluation_id, criterion_id, section, type, weight, answer_json, "
                "applicable, points, explanation, evidence_json, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (eid, c["criterion_id"], c["section"], c["type"], c["weight"], _j(c["answer"]),
                 int(bool(c["applicable"])), c.get("points"), c.get("explanation"), _j(c.get("evidence", {})),
                 _j(c.get("meta", {}))),
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
                      "explanation": c["explanation"], "evidence": json.loads(c["evidence_json"]),
                      **_meta(json.loads(c["meta_json"]))} for c in crit]
    d["llm_call_ids"] = [r[0] for r in conn.execute(
        "SELECT id FROM llm_calls WHERE evaluation_id = ? ORDER BY started_at", (evaluation_id,))]
    d["cost"] = evaluation_cost(conn, evaluation_id)
    return d


def _meta(m: dict) -> dict:
    return {"confidence": m.get("confidence"), "votes": m.get("votes", []), "model_answer": m.get("model_answer"),
            "guards": m.get("guards", []), "hints": m.get("hints", []), "violation_type": m.get("violation_type")}


def evaluation_cost(conn: sqlite3.Connection, evaluation_id: str) -> dict:
    r = conn.execute("SELECT COUNT(*), COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0), "
                     "COALESCE(SUM(cost_usd),0), COALESCE(SUM(status != 'ok'),0) FROM llm_calls "
                     "WHERE evaluation_id = ?", (evaluation_id,)).fetchone()
    return {"calls": r[0], "tokens_in": r[1], "tokens_out": r[2], "usd": round(r[3], 6), "rejected_calls": r[4]}


def find_cached_evaluation(conn: sqlite3.Connection, conversation_id: str, checklist_id: str,
                           checklist_version: int, prompt_version: str, model_name: str | None,
                           votes: int, effort: str | None = None) -> dict | None:
    """Последняя успешная оценка с тем же ключом: диалог, версия чек-листа, версия промпта, модель, effort,
    голоса. not_evaluable кэшируется независимо от модели (LLM не вызывалась)."""
    row = conn.execute(
        "SELECT id FROM evaluations WHERE conversation_id = ? AND checklist_id = ? AND checklist_version = ? "
        "AND prompt_version = ? AND ((status = 'completed' AND model_name = ? AND votes = ? AND effort IS ?) "
        "OR status = 'not_evaluable') ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (conversation_id, checklist_id, checklist_version, prompt_version, model_name, votes, effort)).fetchone()
    return get_evaluation(conn, row[0]) if row else None


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
        _appeal_event(conn, aid, "opened", actor=author)
    return get_appeal(conn, aid)


def _appeal_event(conn: sqlite3.Connection, appeal_id: str, event: str, *, actor: str | None = None,
                  note: str | None = None) -> None:
    conn.execute("INSERT INTO appeal_events(id, appeal_id, event, actor, note, created_at) VALUES (?,?,?,?,?,?)",
                 (new_id("ae"), appeal_id, event, actor, note, now()))


def start_review(conn: sqlite3.Connection, appeal_id: str, reviewer: str | None = None) -> dict:
    """open → in_review: проверяющий взял апелляцию в работу."""
    appeal = get_appeal(conn, appeal_id)
    if appeal["decision"] is not None:
        raise Conflict(f"по апелляции {appeal_id} решение уже принято")
    if appeal["status"] == "in_review":
        raise Conflict(f"апелляция {appeal_id} уже на рассмотрении")
    with conn:
        _appeal_event(conn, appeal_id, "in_review", actor=reviewer)
    return get_appeal(conn, appeal_id)


def insert_decision(conn: sqlite3.Connection, appeal_id: str, decision: str, *, new_answer: Any = None,
                    reason: str | None = None, reviewer: str | None = None) -> dict:
    """Решение по апелляции: confirm (подтвердить) или change (изменить — обязательны new_answer и reason).
    Проверка new_answer по критерию — в вызывающем коде (scoring.validate_answer). Решение из статуса open
    допускается: событие in_review записывается автоматически, журнал остаётся полным."""
    appeal = get_appeal(conn, appeal_id)
    if appeal["decision"] is not None:
        raise Conflict(f"по апелляции {appeal_id} решение уже принято")
    if decision not in ("confirm", "change"):
        raise ValidationFailed([{"path": "decision", "message": "confirm или change"}])
    if decision == "change" and (new_answer is None or not (reason and reason.strip())):
        raise ValidationFailed([{"path": "new_answer/reason", "message": "для change нужны новый ответ и причина"}])
    with conn:
        if appeal["status"] == "open":
            _appeal_event(conn, appeal_id, "in_review", actor=reviewer, note="автоматически при решении")
        conn.execute("INSERT INTO appeal_decisions(id, appeal_id, decision, new_answer_json, reason, reviewer, "
                     "decided_at) VALUES (?,?,?,?,?,?,?)",
                     (new_id("dc"), appeal_id, decision, None if decision == "confirm" else _j(new_answer),
                      reason, reviewer, now()))
        _appeal_event(conn, appeal_id, "decided", actor=reviewer,
                      note=decision if decision == "confirm" else f"change → {_j(new_answer)}: {reason}")
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
    ev = conn.execute("SELECT conversation_id FROM evaluations WHERE id = ?", (d["evaluation_id"],)).fetchone()
    d["conversation_id"] = ev[0] if ev else None
    cr = conn.execute("SELECT answer_json, points FROM criterion_results WHERE evaluation_id = ? AND criterion_id = ?",
                      (d["evaluation_id"], d["criterion_id"])).fetchone()
    d["original"] = {"answer": json.loads(cr["answer_json"]), "points": cr["points"]} if cr else None
    dec = conn.execute("SELECT * FROM appeal_decisions WHERE appeal_id = ?", (d["id"],)).fetchone()
    d["events"] = [dict(e) for e in conn.execute(
        "SELECT event, actor, note, created_at FROM appeal_events WHERE appeal_id = ? ORDER BY created_at, rowid",
        (d["id"],))]
    if dec:
        d["status"] = "confirmed" if dec["decision"] == "confirm" else "changed"
    elif any(e["event"] == "in_review" for e in d["events"]):
        d["status"] = "in_review"
    else:
        d["status"] = "open"
    d["decision"] = None if not dec else {
        "decision": dec["decision"],
        "new_answer": None if dec["new_answer_json"] is None else json.loads(dec["new_answer_json"]),
        "reason": dec["reason"], "reviewer": dec["reviewer"], "decided_at": dec["decided_at"],
    }
    return d


def change_decisions(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """{evaluation_id: {criterion_id: new_answer}} по решениям «change», в порядке принятия (последнее побеждает)."""
    out: dict[str, dict[str, Any]] = {}
    for r in conn.execute("SELECT a.evaluation_id, a.criterion_id, d.new_answer_json FROM appeal_decisions d "
                          "JOIN appeals a ON a.id = d.appeal_id WHERE d.decision = 'change' "
                          "ORDER BY d.decided_at, d.rowid"):
        out.setdefault(r[0], {})[r[1]] = json.loads(r[2])
    return out


def query_appeals(conn: sqlite3.Connection, *, status: str | None = None, evaluation_id: str | None = None,
                  conversation_id: str | None = None, criterion_id: str | None = None) -> list[dict]:
    sql = ("SELECT a.* FROM appeals a JOIN evaluations e ON e.id = a.evaluation_id "
           "LEFT JOIN appeal_decisions d ON d.appeal_id = a.id WHERE 1=1")
    args: list[Any] = []
    if evaluation_id:
        sql += " AND a.evaluation_id = ?"; args.append(evaluation_id)
    if conversation_id:
        sql += " AND e.conversation_id = ?"; args.append(conversation_id)
    if criterion_id:
        sql += " AND a.criterion_id = ?"; args.append(criterion_id)
    in_review = "EXISTS (SELECT 1 FROM appeal_events x WHERE x.appeal_id = a.id AND x.event = 'in_review')"
    if status == "open":
        sql += f" AND d.id IS NULL AND NOT {in_review}"
    elif status == "in_review":
        sql += f" AND d.id IS NULL AND {in_review}"
    elif status == "active":                      # open + in_review
        sql += " AND d.id IS NULL"
    elif status == "confirmed":
        sql += " AND d.decision = 'confirm'"
    elif status == "changed":
        sql += " AND d.decision = 'change'"
    elif status == "decided":                     # confirmed + changed
        sql += " AND d.id IS NOT NULL"
    sql += " ORDER BY a.created_at, a.rowid"
    return [_appeal(conn, r) for r in conn.execute(sql, args).fetchall()]


APPEAL_STATUSES = ("open", "in_review", "active", "confirmed", "changed", "decided")


# --- списки и сводки для REST API ----------------------------------------------------------
def _open_appeals_by_evaluation(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT a.evaluation_id, COUNT(*) FROM appeals a LEFT JOIN appeal_decisions d "
                        "ON d.appeal_id = a.id WHERE d.id IS NULL GROUP BY a.evaluation_id").fetchall()
    return {r[0]: r[1] for r in rows}


def _eval_summary(row: sqlite3.Row, open_appeals: dict[str, int]) -> dict:
    return {"id": row["id"], "status": row["status"], "status_reason": row["status_reason"],
            "checklist": {"id": row["checklist_id"], "version": row["checklist_version"]},
            "total": row["total"], "final_total": row["total"], "open_appeals": open_appeals.get(row["id"], 0),
            "batch_id": row["batch_id"], "created_at": row["created_at"]}


def latest_evaluations(conn: sqlite3.Connection, *, checklist_id: str | None = None,
                       checklist_version: int | None = None, batch_id: str | None = None) -> dict[str, dict]:
    """Последняя оценка каждого диалога (по порядку вставки), при необходимости — только с данным чек-листом."""
    where, args = [], []
    if checklist_id:
        where.append("checklist_id = ?"); args.append(checklist_id)
    if checklist_version is not None:
        where.append("checklist_version = ?"); args.append(checklist_version)
    if batch_id:
        where.append("batch_id = ?"); args.append(batch_id)
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT e.* FROM evaluations e JOIN (SELECT conversation_id, MAX(rowid) AS rid FROM evaluations{cond} "
        f"GROUP BY conversation_id) m ON m.rid = e.rowid", args).fetchall()
    opens = _open_appeals_by_evaluation(conn)
    return {r["conversation_id"]: _eval_summary(r, opens) for r in rows}


def query_evaluations(conn: sqlite3.Connection, *, conversation_id: str | None = None, batch_id: str | None = None,
                      checklist_id: str | None = None, checklist_version: int | None = None,
                      status: str | None = None, limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
    """Оценки без criteria (поля объекта оценки), новые первыми."""
    where, args = [], []
    for col, val in (("conversation_id", conversation_id), ("batch_id", batch_id), ("checklist_id", checklist_id),
                     ("checklist_version", checklist_version), ("status", status)):
        if val is not None:
            where.append(f"{col} = ?"); args.append(val)
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM evaluations{cond}", args).fetchone()[0]
    rows = conn.execute(f"SELECT * FROM evaluations{cond} ORDER BY rowid DESC LIMIT ? OFFSET ?",
                        args + [limit, offset]).fetchall()
    opens = _open_appeals_by_evaluation(conn)
    items = []
    for r in rows:
        d = dict(r)
        for k in ("sections", "flags", "facts"):
            d[k] = json.loads(d.pop(f"{k}_json"))
        d["checklist"] = {"id": d.pop("checklist_id"), "version": d.pop("checklist_version")}
        d["model"] = {"provider": d.pop("model_provider"), "name": d.pop("model_name")}
        d["final_total"] = d["total"]
        d["open_appeals"] = opens.get(d["id"], 0)
        d["cost"] = evaluation_cost(conn, d["id"])
        items.append(d)
    return items, total


def query_llm_calls(conn: sqlite3.Connection, *, evaluation_id: str | None = None, batch_id: str | None = None,
                    conversation_id: str | None = None, status: str | None = None,
                    limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
    where, args = [], []
    for col, val in (("evaluation_id", evaluation_id), ("batch_id", batch_id), ("conversation_id", conversation_id),
                     ("status", status)):
        if val is not None:
            where.append(f"{col} = ?"); args.append(val)
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM llm_calls{cond}", args).fetchone()[0]
    cols = ("id, evaluation_id, conversation_id, batch_id, attempt, provider, model, started_at, latency_ms, "
            "tokens_in, tokens_out, cost_usd, status, validation_errors_json")
    rows = conn.execute(f"SELECT {cols} FROM llm_calls{cond} ORDER BY started_at DESC, rowid DESC LIMIT ? OFFSET ?",
                        args + [limit, offset]).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["validation_errors"] = json.loads(d.pop("validation_errors_json"))
        items.append(d)
    return items, total


def llm_calls_summary(conn: sqlite3.Connection, **filters) -> dict:
    where, args = [], []
    for col in ("evaluation_id", "batch_id", "conversation_id", "status"):
        if filters.get(col) is not None:
            where.append(f"{col} = ?"); args.append(filters[col])
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    r = conn.execute(f"SELECT COUNT(*), COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0), "
                     f"COALESCE(SUM(cost_usd),0), AVG(latency_ms), COALESCE(SUM(attempt > 1),0) "
                     f"FROM llm_calls{cond}", args).fetchone()
    by_model = conn.execute(f"SELECT provider, model, COUNT(*) AS calls, COALESCE(SUM(tokens_in),0) AS tokens_in, "
                            f"COALESCE(SUM(tokens_out),0) AS tokens_out, COALESCE(SUM(cost_usd),0) AS cost_usd "
                            f"FROM llm_calls{cond} GROUP BY provider, model ORDER BY calls DESC", args).fetchall()
    return {"calls": r[0], "tokens_in": r[1], "tokens_out": r[2], "cost_usd": round(r[3], 6),
            "avg_latency_ms": None if r[4] is None else int(r[4]), "retries": r[5],
            "by_model": [dict(m) for m in by_model]}


def dataset_stats(conn: sqlite3.Connection, group_id: str) -> dict:
    channels = {r[0]: r[1] for r in conn.execute(
        "SELECT channel, COUNT(*) FROM conversations WHERE group_id = ? GROUP BY channel", (group_id,))}
    languages = {r[0]: r[1] for r in conn.execute(
        "SELECT language, COUNT(*) FROM conversations WHERE group_id = ? GROUP BY language", (group_id,))}
    evaluated = conn.execute("SELECT COUNT(DISTINCT e.conversation_id) FROM evaluations e JOIN conversations c "
                             "ON c.conversation_id = e.conversation_id WHERE c.group_id = ?", (group_id,)).fetchone()[0]
    return {"channels": channels, "languages": languages, "evaluated": evaluated}


def search_conversations(conn: sqlite3.Connection, *, group_id: str | None = None, channel: str | None = None,
                         language: str | None = None, q: str | None = None) -> list[dict]:
    where, args = [], []
    for col, val in (("group_id", group_id), ("channel", channel), ("language", language)):
        if val is not None:
            where.append(f"{col} = ?"); args.append(val)
    if q:
        where.append("(conversation_id LIKE ? OR transcript_json LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    return [_conv(r) for r in conn.execute(f"SELECT * FROM conversations{cond} ORDER BY group_id, conversation_id", args)]


# --- пакеты -----------------------------------------------------------------------------
BATCH_ITEM_DONE = ("completed", "not_evaluable", "failed", "cancelled")


def insert_batch(conn: sqlite3.Connection, *, checklist_id: str, checklist_version: int,
                 conversation_ids: list[str], group_id: str | None = None, concurrency: int = 4) -> dict:
    bid = new_id("bt")
    with conn:
        conn.execute("INSERT INTO batches(id, group_id, checklist_id, checklist_version, status, concurrency, "
                     "created_at) VALUES (?,?,?,?,?,?,?)",
                     (bid, group_id, checklist_id, checklist_version, "queued", concurrency, now()))
        conn.executemany("INSERT INTO batch_items(batch_id, conversation_id, status) VALUES (?,?,?)",
                         [(bid, cid, "queued") for cid in conversation_ids])
    return get_batch(conn, bid)


def set_batch_status(conn: sqlite3.Connection, batch_id: str, status: str, *, error: str | None = None) -> None:
    finished = now() if status in ("completed", "cancelled", "failed") else None
    with conn:
        conn.execute("UPDATE batches SET status = ?, error = COALESCE(?, error), finished_at = COALESCE(?, finished_at) "
                     "WHERE id = ?", (status, error, finished, batch_id))


def set_batch_item(conn: sqlite3.Connection, batch_id: str, conversation_id: str, status: str, *,
                   evaluation_id: str | None = None, total: float | None = None, error: str | None = None) -> None:
    with conn:
        conn.execute("UPDATE batch_items SET status = ?, evaluation_id = ?, total = ?, error = ? "
                     "WHERE batch_id = ? AND conversation_id = ?",
                     (status, evaluation_id, total, error, batch_id, conversation_id))


def request_batch_cancel(conn: sqlite3.Connection, batch_id: str) -> dict:
    get_batch(conn, batch_id)
    with conn:
        conn.execute("UPDATE batches SET cancel_requested = 1 WHERE id = ?", (batch_id,))
        conn.execute("UPDATE batch_items SET status = 'cancelled' WHERE batch_id = ? AND status = 'queued'", (batch_id,))
        conn.execute("UPDATE batches SET status = 'cancelled', finished_at = ? WHERE id = ? "
                     "AND status IN ('queued') ", (now(), batch_id))
    return get_batch(conn, batch_id)


def batch_cancel_requested(conn: sqlite3.Connection, batch_id: str) -> bool:
    row = conn.execute("SELECT cancel_requested FROM batches WHERE id = ?", (batch_id,)).fetchone()
    return bool(row and row[0])


def _batch(conn: sqlite3.Connection, row: sqlite3.Row, with_items: bool) -> dict:
    d = dict(row)
    items = [dict(r) for r in conn.execute("SELECT conversation_id, status, evaluation_id, total, error FROM batch_items "
                                           "WHERE batch_id = ? ORDER BY rowid", (d["id"],))]
    counts = {s: sum(1 for i in items if i["status"] == s)
              for s in ("queued", "running", "completed", "not_evaluable", "failed", "cancelled")}
    total = len(items)
    done = sum(counts[s] for s in BATCH_ITEM_DONE)
    cost = conn.execute("SELECT COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0), COALESCE(SUM(cost_usd),0) "
                        "FROM llm_calls WHERE batch_id = ?", (d["id"],)).fetchone()
    out = {"batch_id": d["id"], "status": d["status"], "group_id": d["group_id"],
           "checklist": {"id": d["checklist_id"], "version": d["checklist_version"]},
           "created_at": d["created_at"], "finished_at": d["finished_at"], "error": d["error"],
           "concurrency": d["concurrency"], "cancel_requested": bool(d["cancel_requested"]),
           "progress": {"total": total, "done": done, **counts,
                        "percent": round(done / total * 100, 2) if total else 100.0},
           "cost": {"tokens_in": cost[0], "tokens_out": cost[1], "usd": round(cost[2], 6)}}
    if with_items:
        out["items"] = items
    return out


def get_batch(conn: sqlite3.Connection, batch_id: str) -> dict:
    row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if not row:
        raise NotFound(f"пакет {batch_id} не найден")
    return _batch(conn, row, with_items=True)


def list_batches(conn: sqlite3.Connection) -> list[dict]:
    return [_batch(conn, r, with_items=False) for r in conn.execute("SELECT * FROM batches ORDER BY rowid DESC")]
