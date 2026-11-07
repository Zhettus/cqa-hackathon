"""REST API по docs/api_contract.md. Запуск: uvicorn backend.main:app --reload → http://localhost:8000

Фронтенд (frontend/) раздаётся как статика под /ui/. Все ошибки — в формате
{"error": {"code", "message", "details"}}. Соединение с SQLite — на каждый запрос.
"""
from __future__ import annotations

import csv
import io
import json
import mimetypes
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterator, Literal

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend import data_source, db
from backend.appeals import apply_final_totals, final_result
from backend.checklist import validate_checklist
from backend.config import ROOT, get_settings
from backend.evaluator import Evaluator
from backend.llm import DEFAULT_MODELS, LLMError, get_provider
from backend.normalize import normalize_call, normalizer_version, text_hash
from backend.preprocess import preprocess
from backend.scoring import AnswerError, validate_answer

VERSION = "0.1.0"
LANGUAGES = ["RU", "KZ", "EN"]
CHANNELS = ["agent chat", "voice call"]
FRONTEND_DIR = ROOT / "frontend"
AUDIO_EXTS = (".mp3", ".wav", ".ogg", ".m4a", ".webm", ".opus", ".aac", ".flac")


# --- ошибки ---------------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details or {}


def _error(status: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message,
                                                               "details": details or {}}})


# --- тела запросов ----------------------------------------------------------------------------
class ImportRequest(BaseModel):
    group_id: str
    prefer: Literal["remote", "local"] = "remote"


class EvaluateRequest(BaseModel):
    conversation_id: str
    checklist_id: str = "chat-quality"
    checklist_version: int | None = None


class BatchRequest(BaseModel):
    group_id: str | None = None
    conversation_ids: list[str] | None = None
    checklist_id: str = "chat-quality"
    checklist_version: int | None = None
    only_not_evaluated: bool = False
    concurrency: int = Field(default=4, ge=1, le=32)


class AppealRequest(BaseModel):
    criterion_id: str
    comment: str
    author: str | None = None


class DecisionRequest(BaseModel):
    decision: Literal["confirm", "change"]
    new_answer: Any = None
    reason: str | None = None
    reviewer: str | None = None


class ReviewRequest(BaseModel):
    reviewer: str | None = None


# --- приложение ------------------------------------------------------------------------------
def create_app(db_path: str | Path | None = None, data_dir: str | Path | None = None,
               provider=None) -> FastAPI:
    """Фабрика: тесты передают временную БД и подставной провайдер."""
    from backend.batches import BatchRunner

    s = get_settings()
    app = FastAPI(title="CQA — Conversation Quality Analytics", version=VERSION)
    app.state.db_path = Path(db_path) if db_path else s.db_path
    app.state.data_dir = Path(data_dir) if data_dir else s.data_dir
    app.state.provider = provider

    def provider_factory():
        if app.state.provider is None:
            app.state.provider = get_provider()
        return app.state.provider

    app.state.provider_factory = provider_factory
    app.state.batches = BatchRunner(app.state.db_path, provider_factory)

    conn = db.connect(app.state.db_path)
    db.init_db(conn)
    conn.close()

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, e: ApiError):
        return _error(e.status, e.code, e.message, e.details)

    @app.exception_handler(db.NotFound)
    async def _not_found(_: Request, e: db.NotFound):
        return _error(404, "not_found", str(e))

    @app.exception_handler(db.Conflict)
    async def _conflict(_: Request, e: db.Conflict):
        return _error(409, "conflict", str(e))

    @app.exception_handler(db.ValidationFailed)
    async def _invalid(_: Request, e: db.ValidationFailed):
        return _error(422, "validation_failed", "Данные не прошли проверку", {"errors": e.errors})

    @app.exception_handler(data_source.DataSourceError)
    async def _source(_: Request, e: data_source.DataSourceError):
        return _error(502, "data_source_error", str(e))

    @app.exception_handler(LLMError)
    async def _llm(_: Request, e: LLMError):
        return _error(502, f"llm_{e.code}", str(e))

    @app.exception_handler(RequestValidationError)
    async def _request_invalid(_: Request, e: RequestValidationError):
        errors = [{"path": ".".join(str(p) for p in err.get("loc", [])), "message": err.get("msg", "")}
                  for err in e.errors()]
        return _error(400, "bad_request", "Некорректные параметры запроса", {"errors": errors})

    @app.exception_handler(HTTPException)
    async def _http(_: Request, e: HTTPException):
        return _error(e.status_code, "http_error", str(e.detail))

    app.include_router(router)
    FRONTEND_DIR.mkdir(exist_ok=True)
    app.mount("/ui", StaticFiles(directory=FRONTEND_DIR, html=True), name="ui")

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/ui/")

    return app


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = db.connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


router = APIRouter()
Conn = Depends(get_conn)


# --- 9. служебное ------------------------------------------------------------------------------
@router.get("/api/health")
async def health():
    return {"status": "ok"}


@router.get("/api/meta")
async def meta():
    kind = (os.getenv("LLM_PROVIDER") or "anthropic").lower()
    return {"llm": {"provider": kind, "model": os.getenv("LLM_MODEL") or DEFAULT_MODELS.get(kind)},
            "data_api_configured": bool(get_settings().data_api_url), "languages": LANGUAGES,
            "channels": CHANNELS, "version": VERSION}


# --- 1. наборы данных ----------------------------------------------------------------------
def _dataset(conn: sqlite3.Connection, d: dict) -> dict:
    return {**d, **db.dataset_stats(conn, d["group_id"])}


@router.post("/api/datasets/import", status_code=201)
async def import_dataset(body: ImportRequest, conn: sqlite3.Connection = Conn):
    res = data_source.import_group(conn, body.group_id, prefer=body.prefer)
    return {**res, "skipped": []}


@router.post("/api/datasets/upload", status_code=201)
async def upload_dataset(payload: Any = Body(...), conn: sqlite3.Connection = Conn):
    res = data_source.import_upload(conn, payload)
    return {**res, "skipped": []}


@router.get("/api/datasets")
async def list_datasets(conn: sqlite3.Connection = Conn):
    items = [_dataset(conn, d) for d in db.list_datasets(conn)]
    return {"items": items, "total": len(items)}


@router.get("/api/datasets/{group_id}")
async def get_dataset(group_id: str, conn: sqlite3.Connection = Conn):
    return _dataset(conn, db.get_dataset(conn, group_id))


# --- 2. диалоги ----------------------------------------------------------------------------
def _audio_file(request: Request, conversation_id: str) -> Path | None:
    folder = request.app.state.data_dir / "audio"
    for ext in AUDIO_EXTS:
        p = folder / f"{conversation_id}{ext}"
        if p.exists():
            return p
    return None


def _segments(messages: list) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if out and out[-1]["kind"] == m.segment:
            out[-1]["to_n"] = m.idx
        else:
            out.append({"kind": m.segment, "from_n": m.idx, "to_n": m.idx})
    return out


@router.get("/api/conversations")
async def list_conversations(request: Request, group_id: str | None = None, channel: str | None = None,
                             language: str | None = None, status: str | None = None, appeal: str | None = None,
                             q: str | None = None, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                             conn: sqlite3.Connection = Conn):
    if status is not None and status not in ("not_evaluated", "completed", "not_evaluable", "failed"):
        raise ApiError(400, "bad_request", "status: not_evaluated | completed | not_evaluable | failed")
    if appeal is not None and appeal not in ("open", "decided", "none"):
        raise ApiError(400, "bad_request", "appeal: open | decided | none")
    latest = db.latest_evaluations(conn)
    apply_final_totals(conn, latest.values())
    decided = {r[0] for r in conn.execute("SELECT DISTINCT a.evaluation_id FROM appeals a JOIN appeal_decisions d "
                                          "ON d.appeal_id = a.id")}
    items = []
    for c in db.search_conversations(conn, group_id=group_id, channel=channel, language=language, q=q):
        ev = latest.get(c["conversation_id"])
        st = ev["status"] if ev else "not_evaluated"
        if status and st != status:
            continue
        # open — есть нерешённые апелляции (open или in_review); decided — все решены
        ap = "none" if not ev else ("open" if ev["open_appeals"] else ("decided" if ev["id"] in decided else "none"))
        if appeal and ap != appeal:
            continue
        pre = preprocess(c)
        items.append({"conversation_id": c["conversation_id"], "group_id": c["group_id"], "channel": c["channel"],
                      "language": c["language"], "messages": len(pre.messages),
                      "operator_messages": pre.facts.operator_messages,
                      "has_audio": _audio_file(request, c["conversation_id"]) is not None,
                      "latest_evaluation": ev})
    return {"items": items[offset:offset + limit], "total": len(items)}


@router.get("/api/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, request: Request, normalize: bool = True,
                           conn: sqlite3.Connection = Conn):
    """Карточка диалога. Для звонков text_clean/asr_uncertain отдаются всегда: нормализация берётся из кэша,
    при его отсутствии выполняется (один вызов модели, затем кэш); normalize=0 — не вызывать модель."""
    c = db.get_conversation(conn, conversation_id)
    pre = preprocess(c)
    audio = _audio_file(request, conversation_id)
    first_op = next((m.idx for m in pre.messages if m.counts_for_operator), None)
    norm, norm_error = None, None
    if c["channel"] == "voice call":
        norm = db.latest_normalization(conn, conversation_id, text_hash(pre))
        if norm is None and normalize:
            try:
                provider = request.app.state.provider_factory()
                items, _ = await normalize_call(conn, provider, pre)
                norm = {"version": normalizer_version(), "model": provider.model, "created_at": db.now(), "items": items}
            except Exception as e:  # noqa: BLE001 — карточка должна открываться и без корректора
                norm_error = f"{type(e).__name__}: {e}"
    nitems = norm["items"] if norm else {}
    return {
        "conversation_id": conversation_id, "group_id": c["group_id"], "channel": c["channel"],
        "language": c["language"],
        "audio": None if audio is None else {"available": True, "url": f"/api/conversations/{conversation_id}/audio",
                                             "content_type": mimetypes.guess_type(audio.name)[0] or "audio/mpeg"},
        "operator_start_n": first_op, "segments": _segments(pre.messages), "facts": pre.facts.model_dump(),
        "normalization": None if norm is None else {k: norm[k] for k in ("version", "model", "created_at")},
        "normalization_error": norm_error,
        "messages": [{"n": m.idx, "timestamp": m.timestamp, "speaker": m.speaker, "text": m.text,
                      "clean_text": m.clean_text, "t_s": m.t_s, "audio_offset_s": m.audio_offset_s,
                      "segment": m.segment, "flags": m.flags, "counts_for_operator": m.counts_for_operator,
                      "event": m.event,
                      "text_clean": nitems.get(m.idx, {}).get("text_clean"),         # корректор ASR (этап 5)
                      "asr_uncertain": bool(nitems.get(m.idx, {}).get("asr_uncertain", False))}
                     for m in pre.messages],
    }


@router.get("/api/conversations/{conversation_id}/audio")
async def get_audio(conversation_id: str, request: Request, conn: sqlite3.Connection = Conn):
    db.get_conversation(conn, conversation_id)
    audio = _audio_file(request, conversation_id)
    if audio is None:
        raise ApiError(404, "not_found", f"аудио для {conversation_id} нет в data/audio/")
    return FileResponse(audio, media_type=mimetypes.guess_type(audio.name)[0] or "audio/mpeg")


# --- 3. чек-листы ----------------------------------------------------------------------------
def _checklist_body(body: dict, checklist_id: str | None = None) -> dict:
    cl = {k: v for k, v in body.items() if k not in ("base_version", "comment", "author", "version", "created_at",
                                                     "is_latest")}
    if checklist_id:
        cl["id"] = checklist_id
    return cl


@router.get("/api/checklists")
async def list_checklists(conn: sqlite3.Connection = Conn):
    items = db.list_checklists(conn)
    return {"items": items, "total": len(items)}


@router.post("/api/checklists", status_code=201)
async def create_checklist(body: dict, conn: sqlite3.Connection = Conn):
    cl = _checklist_body(body)
    errors = validate_checklist(cl)
    if errors:
        raise db.ValidationFailed(errors)
    if conn.execute("SELECT 1 FROM checklists WHERE id = ?", (cl["id"],)).fetchone():
        raise db.Conflict(f"чек-лист {cl['id']} уже существует — создайте новую версию")
    return db.insert_checklist_version(conn, cl, author=body.get("author"), comment=body.get("comment"), version=1)


@router.post("/api/checklists/validate")
async def validate_checklist_body(body: dict):
    cl = _checklist_body(body)
    cl.setdefault("id", "draft")                      # тело новой версии может не содержать id
    errors = validate_checklist(cl)
    return {"valid": not errors, "errors": errors}


@router.get("/api/checklists/{checklist_id}")
async def get_checklist(checklist_id: str, conn: sqlite3.Connection = Conn):
    return db.get_checklist(conn, checklist_id)


@router.get("/api/checklists/{checklist_id}/versions")
async def list_versions(checklist_id: str, conn: sqlite3.Connection = Conn):
    return db.list_checklist_versions(conn, checklist_id)


@router.post("/api/checklists/{checklist_id}/versions", status_code=201)
async def create_version(checklist_id: str, body: dict, conn: sqlite3.Connection = Conn):
    db.get_checklist(conn, checklist_id)
    if "base_version" not in body:
        raise ApiError(400, "bad_request", "нужно поле base_version")
    return db.insert_checklist_version(conn, _checklist_body(body, checklist_id), base_version=int(body["base_version"]),
                                       author=body.get("author"), comment=body.get("comment"))


@router.get("/api/checklists/{checklist_id}/versions/{version}")
async def get_version(checklist_id: str, version: int, conn: sqlite3.Connection = Conn):
    return db.get_checklist(conn, checklist_id, version)


@router.get("/api/checklists/{checklist_id}/versions/{a}/diff/{b}")
async def diff_versions(checklist_id: str, a: int, b: int, conn: sqlite3.Connection = Conn):
    va, vb = db.get_checklist(conn, checklist_id, a), db.get_checklist(conn, checklist_id, b)
    ca, cb = {c["id"]: c for c in va["criteria"]}, {c["id"]: c for c in vb["criteria"]}
    changed = [{"criterion_id": cid, "fields": sorted(k for k in set(ca[cid]) | set(cb[cid])
                                                      if ca[cid].get(k) != cb[cid].get(k))}
               for cid in ca if cid in cb and ca[cid] != cb[cid]]
    other = sorted(k for k in ("name", "sections", "settings") if va.get(k) != vb.get(k))
    return {"from": a, "to": b, "added": [cid for cid in cb if cid not in ca],
            "removed": [cid for cid in ca if cid not in cb], "changed": changed, "other_changed": other}


# --- 4. оценка -------------------------------------------------------------------------------
def _with_titles(conn: sqlite3.Connection, ev: dict) -> tuple[dict, list[dict], dict | None]:
    """Объект оценки по контракту (title критерия, final_total, open_appeals) + апелляции + final."""
    try:
        cl = db.get_checklist(conn, ev["checklist"]["id"], ev["checklist"]["version"])
        titles = {c["id"]: c.get("title", "") for c in cl["criteria"]}
    except db.NotFound:
        cl, titles = None, {}
    for c in ev.get("criteria", []):
        c.setdefault("title", titles.get(c["criterion_id"], ""))
    appeals = db.list_appeals(conn, ev["id"])
    final = final_result(conn, ev, appeals, cl) if cl else None
    ev["final_total"] = final["total"] if final else ev.get("total")
    ev["open_appeals"] = sum(1 for a in appeals if a["status"] in ("open", "in_review"))
    return ev, appeals, final


@router.post("/api/evaluations", status_code=201)
async def create_evaluation(body: EvaluateRequest, request: Request, conn: sqlite3.Connection = Conn):
    evaluator = Evaluator(conn, request.app.state.provider_factory())
    ev = await evaluator.evaluate(body.conversation_id, body.checklist_id, body.checklist_version)
    return _with_titles(conn, ev)[0]


@router.get("/api/evaluations")
async def list_evaluations(conversation_id: str | None = None, batch_id: str | None = None,
                           checklist_id: str | None = None, checklist_version: int | None = None,
                           status: str | None = None, limit: int = Query(50, ge=1, le=500),
                           offset: int = Query(0, ge=0), conn: sqlite3.Connection = Conn):
    items, total = db.query_evaluations(conn, conversation_id=conversation_id, batch_id=batch_id,
                                        checklist_id=checklist_id, checklist_version=checklist_version,
                                        status=status, limit=limit, offset=offset)
    apply_final_totals(conn, items)
    return {"items": items, "total": total}


@router.get("/api/evaluations/{evaluation_id}")
async def get_evaluation(evaluation_id: str, conn: sqlite3.Connection = Conn):
    """Исходная оценка (неизменна), апелляции с журналом и финальный итог с учётом решений."""
    ev, appeals, final = _with_titles(conn, db.get_evaluation(conn, evaluation_id))
    return {"evaluation": ev, "appeals": appeals, "final": final}


# --- 5. пакетная оценка ----------------------------------------------------------------------
@router.post("/api/batches", status_code=202)
async def create_batch(body: BatchRequest, request: Request, conn: sqlite3.Connection = Conn):
    cl = db.get_checklist(conn, body.checklist_id, body.checklist_version)
    if body.conversation_ids:
        ids = list(dict.fromkeys(body.conversation_ids))
        for cid in ids:
            db.get_conversation(conn, cid)
    elif body.group_id:
        db.get_dataset(conn, body.group_id)
        ids = [c["conversation_id"] for c in db.list_conversations(conn, body.group_id)]
    else:
        raise ApiError(400, "bad_request", "нужен group_id или conversation_ids")
    if body.only_not_evaluated:
        done = db.latest_evaluations(conn, checklist_id=cl["id"], checklist_version=cl["version"])
        ids = [cid for cid in ids if cid not in done]
    batch = db.insert_batch(conn, checklist_id=cl["id"], checklist_version=cl["version"], conversation_ids=ids,
                            group_id=body.group_id, concurrency=body.concurrency)
    if ids:
        request.app.state.batches.start(batch["batch_id"])
        status = "running"
    else:
        db.set_batch_status(conn, batch["batch_id"], "completed")
        status = "completed"
    return {"batch_id": batch["batch_id"], "status": status, "total": len(ids)}


@router.get("/api/batches")
async def list_batches(conn: sqlite3.Connection = Conn):
    items = db.list_batches(conn)
    return {"items": items, "total": len(items)}


@router.get("/api/batches/{batch_id}")
async def get_batch(batch_id: str, conn: sqlite3.Connection = Conn):
    return db.get_batch(conn, batch_id)


@router.post("/api/batches/{batch_id}/cancel")
async def cancel_batch(batch_id: str, conn: sqlite3.Connection = Conn):
    return db.request_batch_cancel(conn, batch_id)


# --- 6. результаты ---------------------------------------------------------------------------
@router.get("/api/results")
async def results(group_id: str | None = None, batch_id: str | None = None, checklist_id: str = "chat-quality",
                  checklist_version: int | None = None, channel: str | None = None, language: str | None = None,
                  format: str | None = None, conn: sqlite3.Connection = Conn):
    cl = db.get_checklist(conn, checklist_id, checklist_version)
    latest = db.latest_evaluations(conn, checklist_id=cl["id"], checklist_version=cl["version"], batch_id=batch_id)
    apply_final_totals(conn, latest.values())
    flag_ids = [c["id"] for c in cl["criteria"] if c["type"] == "flag"]
    items = []
    for c in db.search_conversations(conn, group_id=group_id, channel=channel, language=language):
        ev = latest.get(c["conversation_id"])
        if not ev:
            continue
        answers = {r["criterion_id"]: json.loads(r["answer_json"]) for r in conn.execute(
            "SELECT criterion_id, answer_json FROM criterion_results WHERE evaluation_id = ?", (ev["id"],))}
        final_answers = ev.get("final_answers") or answers
        items.append({"conversation_id": c["conversation_id"], "channel": c["channel"], "language": c["language"],
                      "evaluation_id": ev["id"], "status": ev["status"], "total": ev["total"],
                      "final_total": ev["final_total"], "answers": answers, "final_answers": final_answers,
                      "flags": [f for f in flag_ids if final_answers.get(f) == "yes"],
                      "open_appeals": ev["open_appeals"]})
    totals = [i["total"] for i in items if i["total"] is not None]
    summary = {"count": len(items), "avg_total": round(sum(totals) / len(totals), 2) if totals else None,
               "not_evaluable": sum(i["status"] == "not_evaluable" for i in items),
               "failed": sum(i["status"] == "failed" for i in items), "flagged": sum(bool(i["flags"]) for i in items)}
    if format == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        crit_ids = [c["id"] for c in cl["criteria"]]
        w.writerow(["conversation_id", "channel", "language", "evaluation_id", "status", "total", "final_total",
                    *crit_ids, "flags", "open_appeals"])
        for i in items:
            w.writerow([i["conversation_id"], i["channel"], i["language"], i["evaluation_id"], i["status"],
                        i["total"], i["final_total"], *[i["answers"].get(k, "") for k in crit_ids],
                        ";".join(i["flags"]), i["open_appeals"]])
        return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="results_{cl["id"]}_v{cl["version"]}.csv"'})
    return {"checklist": {"id": cl["id"], "version": cl["version"],
                          "criteria": [{"id": c["id"], "title": c.get("title", ""), "type": c["type"]}
                                       for c in cl["criteria"]]},
            "items": items, "summary": summary, "total": len(items)}


# --- 7. апелляции: open → in_review → confirmed | changed; журнал событий; final через scoring ----------
@router.post("/api/evaluations/{evaluation_id}/appeals", status_code=201)
async def create_appeal(evaluation_id: str, body: AppealRequest, conn: sqlite3.Connection = Conn):
    return db.insert_appeal(conn, evaluation_id, body.criterion_id, body.comment, body.author)


@router.post("/api/appeals/{appeal_id}/review")
async def review_appeal(appeal_id: str, body: ReviewRequest | None = None, conn: sqlite3.Connection = Conn):
    """Проверяющий берёт апелляцию в работу: open → in_review."""
    return db.start_review(conn, appeal_id, body.reviewer if body else None)


@router.get("/api/appeals")
async def list_appeals(status: str | None = None, evaluation_id: str | None = None,
                       conversation_id: str | None = None, criterion_id: str | None = None,
                       conn: sqlite3.Connection = Conn):
    if status is not None and status not in db.APPEAL_STATUSES:
        raise ApiError(400, "bad_request", "status: " + " | ".join(db.APPEAL_STATUSES))
    items = db.query_appeals(conn, status=status, evaluation_id=evaluation_id, conversation_id=conversation_id,
                             criterion_id=criterion_id)
    return {"items": items, "total": len(items)}


@router.get("/api/appeals/{appeal_id}")
async def get_appeal(appeal_id: str, conn: sqlite3.Connection = Conn):
    return db.get_appeal(conn, appeal_id)


@router.post("/api/appeals/{appeal_id}/decision", status_code=201)
async def decide_appeal(appeal_id: str, body: DecisionRequest, conn: sqlite3.Connection = Conn):
    appeal = db.get_appeal(conn, appeal_id)
    new_answer = body.new_answer
    if body.decision == "change":
        ev = db.get_evaluation(conn, appeal["evaluation_id"])
        cl = db.get_checklist(conn, ev["checklist"]["id"], ev["checklist"]["version"])
        crit = next((c for c in cl["criteria"] if c["id"] == appeal["criterion_id"]), None)
        if crit is None:
            raise db.NotFound(f"критерия {appeal['criterion_id']} нет в чек-листе оценки")
        try:
            new_answer = validate_answer(crit, body.new_answer)
        except AnswerError as e:
            raise db.ValidationFailed([{"path": "new_answer", "message": str(e)}])
    decided = db.insert_decision(conn, appeal_id, body.decision, new_answer=new_answer, reason=body.reason,
                                 reviewer=body.reviewer)
    ev = db.get_evaluation(conn, appeal["evaluation_id"])
    return {"appeal": decided, "final": final_result(conn, ev)}


# --- 8. логи LLM -----------------------------------------------------------------------------
@router.get("/api/llm-calls/summary")
async def llm_summary(evaluation_id: str | None = None, batch_id: str | None = None,
                      conversation_id: str | None = None, status: str | None = None,
                      conn: sqlite3.Connection = Conn):
    return db.llm_calls_summary(conn, evaluation_id=evaluation_id, batch_id=batch_id,
                                conversation_id=conversation_id, status=status)


@router.get("/api/llm-calls")
async def list_llm_calls(evaluation_id: str | None = None, batch_id: str | None = None,
                         conversation_id: str | None = None, status: str | None = None,
                         limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                         conn: sqlite3.Connection = Conn):
    items, total = db.query_llm_calls(conn, evaluation_id=evaluation_id, batch_id=batch_id,
                                      conversation_id=conversation_id, status=status, limit=limit, offset=offset)
    return {"items": items, "total": total}


@router.get("/api/llm-calls/{call_id}")
async def get_llm_call(call_id: str, conn: sqlite3.Connection = Conn):
    return db.get_llm_call(conn, call_id)


app = create_app()          # для uvicorn backend.main:app
