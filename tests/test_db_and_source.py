import json
import sqlite3

import httpx
import pytest

from backend import data_source, db
from backend.checklist import validate_checklist
from tests.conftest import ROOT


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    yield c
    c.close()


def test_default_checklist_matches_spec(checklist_v1):
    import re
    spec = (ROOT / "docs" / "SPEC.md").read_text(encoding="utf-8")
    spec_cl = json.loads(re.search(r"## Исходный чек-лист\s*```json\n(.*?)```", spec, re.S).group(1))
    for k in ("name", "version", "sections"):
        assert checklist_v1[k] == spec_cl[k]
    # guards и hints — наши настраиваемые расширения; остальное дословно из SPEC
    ours = [{k: v for k, v in c.items() if k not in ("guards", "hints")} for c in checklist_v1["criteria"]]
    assert ours == spec_cl["criteria"]
    assert checklist_v1["settings"]["first_response_threshold_s"] == 180
    assert validate_checklist(checklist_v1) == []


def test_seed_v1_once(conn):
    db.init_db(conn)
    assert [v["version"] for v in db.list_checklist_versions(conn, "chat-quality")] == [1]
    assert db.get_checklist(conn, "chat-quality")["is_latest"]


def test_checklist_versions(conn):
    v1 = db.get_checklist(conn, "chat-quality", 1)
    body = {k: v1[k] for k in ("id", "name", "sections", "criteria", "settings")}
    body["criteria"][5]["description"] = "Новая формулировка"
    v2 = db.insert_checklist_version(conn, body, base_version=1, author="qa", comment="c06")
    assert v2["version"] == 2 and v2["is_latest"]
    assert db.get_checklist(conn, "chat-quality", 1)["criteria"][5]["description"] != "Новая формулировка"
    with pytest.raises(db.Conflict):
        db.insert_checklist_version(conn, body, base_version=1)
    bad = json.loads(json.dumps(body))
    bad["criteria"][0]["na_allowed"] = True
    bad["criteria"][0].pop("na_condition", None)
    with pytest.raises(db.ValidationFailed):
        db.insert_checklist_version(conn, bad)
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE checklists SET json='{}' WHERE version=1")


def _store(conn, records):
    db.save_dataset(conn, "DEV", "local", records)


def test_evaluation_appeal_flow(conn, dev_records):
    _store(conn, [dev_records["p1-ru-001"]])
    eid = db.insert_evaluation(conn, {
        "conversation_id": "p1-ru-001", "checklist_id": "chat-quality", "checklist_version": 1,
        "status": "completed", "total": 100.0, "facts": {"operator_messages": 2}},
        [{"criterion_id": "c04", "section": "safety", "type": "scale", "weight": 20, "answer": 20,
          "applicable": True, "points": 20.0, "explanation": "…", "evidence": {"kind": "quote"}}])
    ev = db.get_evaluation(conn, eid)
    assert ev["checklist"] == {"id": "chat-quality", "version": 1} and ev["criteria"][0]["answer"] == 20
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE evaluations SET total = 1 WHERE id = ?", (eid,))
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM criterion_results WHERE evaluation_id = ?", (eid,))

    ap = db.insert_appeal(conn, eid, "c04", "не согласен", "op")
    assert ap["status"] == "open"
    with pytest.raises(db.Conflict):
        db.insert_appeal(conn, eid, "c04", "ещё раз")
    with pytest.raises(db.ValidationFailed):
        db.insert_decision(conn, ap["id"], "change", new_answer=15)          # без причины
    dec = db.insert_decision(conn, ap["id"], "change", new_answer=15, reason="деталь", reviewer="qa")
    assert dec["status"] == "decided" and dec["decision"]["new_answer"] == 15
    with pytest.raises(db.Conflict):
        db.insert_decision(conn, ap["id"], "confirm")
    assert db.get_evaluation(conn, eid)["total"] == 100.0                  # исходная оценка неизменна
    assert db.insert_appeal(conn, eid, "c04", "новая")["status"] == "open"  # после решения можно снова


def test_llm_call_log(conn):
    cid = db.insert_llm_call(conn, {"provider": "p", "model": "m", "status": "ok", "tokens_in": 10,
                                    "request": {"system": "s"}, "raw_response": "{}"})
    assert db.get_llm_call(conn, cid)["request"] == {"system": "s"}


def test_transcript_stored_verbatim(conn, dev_records):
    rec = dev_records["p4-ru-001"]
    _store(conn, [rec])
    assert db.get_conversation(conn, "p4-ru-001")["transcript"] == rec["transcript"]


def _api_payload(records):
    return {"error": "", "isSuccess": True,
            "data": {"dataset_group_id": "DEV", "total_records": len(records), "records": records}}


def test_import_remote(conn, dev_records, monkeypatch):
    monkeypatch.setenv("DATA_API_URL", "https://example.test/api")
    recs = [dev_records["p1-ru-001"], dev_records["p4-ru-002"]]
    seen = {}

    def handler(req: httpx.Request):
        seen["group_id"] = req.url.params["group_id"]
        return httpx.Response(200, json=_api_payload(recs))

    res = data_source.import_group(conn, "DEV", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert seen["group_id"] == "DEV" and res["source"] == "remote" and res["imported"] == 2
    assert {c["conversation_id"] for c in db.list_conversations(conn, "DEV")} == {"p1-ru-001", "p4-ru-002"}


def test_import_fallback_local(conn, monkeypatch):
    monkeypatch.setenv("DATA_API_URL", "https://example.test/api")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    res = data_source.import_group(conn, "EDGE", client=client)
    assert res["source"] == "local" and res["imported"] == 12 and res["warnings"]
    assert db.get_dataset(conn, "EDGE")["total_records"] == 12


def test_import_without_api_url(conn, monkeypatch):
    monkeypatch.setenv("DATA_API_URL", "")
    assert data_source.import_group(conn, "DEV")["source"] == "local"


def test_import_errors(conn, monkeypatch):
    monkeypatch.setenv("DATA_API_URL", "")
    with pytest.raises(data_source.DataSourceError):
        data_source.import_group(conn, "NOPE")
    with pytest.raises(data_source.DataSourceError):
        data_source.import_group(conn, "../etc")
    with pytest.raises(data_source.DataSourceError):
        data_source.parse_payload({"isSuccess": False, "error": "bad"})
    with pytest.raises(data_source.DataSourceError):
        data_source.parse_payload([{"conversation_id": "a", "channel": "agent chat",
                                    "transcript": [{"timestamp": "00:00", "speaker": "alien", "text": "x"}]}])
