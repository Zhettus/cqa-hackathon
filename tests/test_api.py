"""REST API по docs/api_contract.md через TestClient: временная БД, подставной провайдер (сеть не используется)."""
import json
import os
import tempfile
import time
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="cqa_api_"))
os.environ["DB_PATH"] = str(_TMP / "module.db")        # чтобы импорт backend.main не трогал data/cqa.db

from fastapi.testclient import TestClient  # noqa: E402

from backend.main import create_app  # noqa: E402
from tests.conftest import ROOT  # noqa: E402
from tests.test_evaluator import OK, ScriptedProvider, answer  # noqa: E402


@pytest.fixture
def client(tmp_path):
    prov = ScriptedProvider({cid: [answer(cid)] for cid in OK})
    app = create_app(db_path=tmp_path / "api.db", data_dir=ROOT / "data", provider=prov)
    with TestClient(app) as c:
        c.provider = prov
        yield c


def _import(client, group_id="EDGE"):
    r = client.post("/api/datasets/import", json={"group_id": group_id, "prefer": "local"})
    assert r.status_code == 201, r.text
    return r.json()


def _evaluate(client, conversation_id="edge-ru-001"):
    r = client.post("/api/evaluations", json={"conversation_id": conversation_id})
    assert r.status_code == 201, r.text
    return r.json()


# --- служебное, ошибки ----------------------------------------------------------------------
def test_health_meta_and_ui(client):
    assert client.get("/api/health").json() == {"status": "ok"}
    m = client.get("/api/meta").json()
    assert m["languages"] == ["RU", "KZ", "EN"] and m["channels"] == ["agent chat", "voice call"] and "llm" in m
    assert client.get("/ui/").status_code == 200 and client.get("/", follow_redirects=False).status_code == 307


def test_error_shape(client):
    r = client.get("/api/conversations/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    r = client.get("/api/conversations?limit=0")
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request" and r.json()["error"]["details"]["errors"]
    r = client.post("/api/datasets/import", json={"group_id": "NOPE", "prefer": "local"})
    assert r.status_code == 502 and r.json()["error"]["code"] == "data_source_error"


# --- 1. наборы -------------------------------------------------------------------------------
def test_datasets_import_upload_list(client, dev_records):
    res = _import(client, "EDGE")
    assert res["source"] == "local" and res["imported"] == 12 and res["skipped"] == []
    recs = [dev_records["p1-ru-001"], dev_records["p4-ru-002"]]
    r = client.post("/api/datasets/upload", json={"data": {"dataset_group_id": "UP", "records": recs}})
    assert r.status_code == 201 and r.json()["source"] == "upload" and r.json()["group_id"] == "UP"
    r = client.post("/api/datasets/upload", json=recs)                     # массив записей → UPLOAD
    assert r.status_code == 201 and r.json()["group_id"] == "UPLOAD"
    r = client.post("/api/datasets/upload", json=[{"conversation_id": "x", "channel": "agent chat", "transcript": [
        {"timestamp": "00:00", "speaker": "alien", "text": "x"}]}])
    assert r.status_code == 502
    ds = client.get("/api/datasets").json()
    assert ds["total"] == 3
    edge = next(d for d in ds["items"] if d["group_id"] == "EDGE")
    assert edge["total_records"] == 12 and edge["channels"]["agent chat"] == 11 and edge["languages"]["KZ"] == 4
    assert edge["evaluated"] == 0
    _evaluate(client)
    assert client.get("/api/datasets/EDGE").json()["evaluated"] == 1
    assert client.get("/api/datasets/NOPE").status_code == 404


# --- 2. диалоги ------------------------------------------------------------------------------
def test_conversations_list_filters(client):
    _import(client, "EDGE")
    r = client.get("/api/conversations", params={"group_id": "EDGE"}).json()
    assert r["total"] == 12 and len(r["items"]) == 12
    item = next(i for i in r["items"] if i["conversation_id"] == "edge-ru-001")
    assert item["latest_evaluation"] is None and item["operator_messages"] > 0 and item["has_audio"] is False
    assert client.get("/api/conversations", params={"language": "KZ"}).json()["total"] == 4
    assert client.get("/api/conversations", params={"channel": "voice call"}).json()["total"] == 1
    assert client.get("/api/conversations", params={"q": "edge-ru-00"}).json()["total"] == 7
    assert client.get("/api/conversations", params={"limit": 5, "offset": 10}).json()["items"].__len__() == 2
    ev = _evaluate(client, "edge-ru-001")
    done = client.get("/api/conversations", params={"status": "completed"}).json()
    assert done["total"] == 1 and done["items"][0]["latest_evaluation"]["id"] == ev["id"]
    assert done["items"][0]["latest_evaluation"]["total"] == 93.08
    assert client.get("/api/conversations", params={"status": "not_evaluated"}).json()["total"] == 11
    assert client.get("/api/conversations", params={"status": "weird"}).status_code == 400
    client.post(f"/api/evaluations/{ev['id']}/appeals", json={"criterion_id": "c04", "comment": "не согласен"})
    assert client.get("/api/conversations", params={"appeal": "open"}).json()["total"] == 1
    assert client.get("/api/conversations", params={"appeal": "none"}).json()["total"] == 11


def test_conversation_detail_and_audio(client, dev_records):
    _import(client, "DEV")
    d = client.get("/api/conversations/p4-ru-002").json()
    assert d["channel"] == "voice call" and d["operator_start_n"] == 3 and d["facts"]["hold_s"] > 0
    kinds = [s["kind"] for s in d["segments"]]
    assert "hold" in kinds and "after_end" in kinds and kinds[0] == "pre_operator"
    m = {x["n"]: x for x in d["messages"]}
    assert m[21]["flags"] == ["client_not_hearing"] and m[21]["counts_for_operator"] is False
    assert m[1]["event"] == "agent_joined" and m[3]["audio_offset_s"] is not None
    assert [x["text"] for x in d["messages"]] == [t["text"] for t in dev_records["p4-ru-002"]["transcript"]]
    chat = client.get("/api/conversations/p1-ru-001").json()
    assert chat["audio"] is None and chat["messages"][0]["audio_offset_s"] is None
    assert client.get("/api/conversations/p1-ru-001/audio").status_code == 404
    if (ROOT / "data" / "audio" / "p4-ru-002.mp3").exists():
        assert d["audio"] == {"available": True, "url": "/api/conversations/p4-ru-002/audio",
                              "content_type": "audio/mpeg"}
        r = client.get("/api/conversations/p4-ru-002/audio", headers={"Range": "bytes=0-99"})
        assert r.status_code == 206 and len(r.content) == 100 and r.headers["content-type"].startswith("audio/mpeg")
    else:
        assert d["audio"] is None


# --- 3. чек-листы ----------------------------------------------------------------------------
def test_checklists_versions_diff_validate(client):
    lst = client.get("/api/checklists").json()
    assert lst["total"] == 1 and lst["items"][0] == {**lst["items"][0], "id": "chat-quality", "version": 1,
                                                     "criteria_count": 8}
    v1 = client.get("/api/checklists/chat-quality").json()
    assert v1["version"] == 1 and v1["is_latest"] and v1["settings"]["first_response_threshold_s"] == 180
    body = {k: v1[k] for k in ("name", "sections", "criteria", "settings")}
    body["criteria"][5]["description"] = "Новая формулировка c06"
    body["criteria"].append({"id": "c09", "section": "conduct", "title": "Новый", "type": "binary", "weight": 5,
                             "na_allowed": False, "description": "Да — …"})
    r = client.post("/api/checklists/chat-quality/versions", json={**body, "base_version": 1, "author": "qa",
                                                                   "comment": "c06 + c09"})
    assert r.status_code == 201 and r.json()["version"] == 2 and r.json()["is_latest"]
    assert client.post("/api/checklists/chat-quality/versions", json={**body, "base_version": 1}).status_code == 409
    assert client.post("/api/checklists/chat-quality/versions", json=body).status_code == 400
    bad = json.loads(json.dumps(body))
    bad["criteria"][0]["na_allowed"] = True
    r = client.post("/api/checklists/chat-quality/versions", json={**bad, "base_version": 2})
    assert r.status_code == 422 and r.json()["error"]["details"]["errors"][0]["path"].endswith("na_condition")
    assert client.post("/api/checklists/validate", json=bad).json()["valid"] is False
    assert client.post("/api/checklists/validate", json=body).json() == {"valid": True, "errors": []}
    vers = client.get("/api/checklists/chat-quality/versions").json()
    assert [v["version"] for v in vers] == [1, 2] and vers[1]["criteria_count"] == 9
    assert client.get("/api/checklists/chat-quality/versions/1").json()["criteria"][5]["description"] != "Новая формулировка c06"
    assert client.get("/api/checklists/chat-quality/versions/9").status_code == 404
    diff = client.get("/api/checklists/chat-quality/versions/1/diff/2").json()
    assert diff["added"] == ["c09"] and diff["removed"] == [] and diff["changed"] == [{"criterion_id": "c06",
                                                                                      "fields": ["description"]}]
    new = {**body, "id": "calls-quality", "name": "Звонки"}
    assert client.post("/api/checklists", json=new).status_code == 201
    assert client.post("/api/checklists", json=new).status_code == 409
    assert client.get("/api/checklists").json()["total"] == 2


# --- 4. оценка -------------------------------------------------------------------------------
def test_evaluate_get_list(client):
    _import(client, "EDGE")
    ev = _evaluate(client, "edge-ru-001")
    assert ev["status"] == "completed" and ev["total"] == 93.08 and ev["checklist"] == {"id": "chat-quality", "version": 1}
    c06 = next(c for c in ev["criteria"] if c["criterion_id"] == "c06")
    assert c06["title"] == "Управление диалогом, скорость и грамотность" and c06["answer"] == 7 and c06["guards"]
    assert ev["cost"]["calls"] == 8 and len(ev["llm_call_ids"]) == 8 and ev["final_total"] == 93.08
    assert ev["open_appeals"] == 0 and ev["effort"] is None and ev["votes"] == 1
    got = client.get(f"/api/evaluations/{ev['id']}").json()
    assert got["evaluation"]["id"] == ev["id"] and got["appeals"] == []
    assert got["final"]["total"] == 93.08 and all(c["source"] == "original" for c in got["final"]["criteria"])
    ne = _evaluate(client, "edge-ru-005")
    assert ne["status"] == "not_evaluable" and ne["total"] is None and ne["criteria"] == [] and ne["cost"]["calls"] == 0
    lst = client.get("/api/evaluations").json()
    assert lst["total"] == 2 and lst["items"][0]["id"] == ne["id"] and "criteria" not in lst["items"][0]
    assert client.get("/api/evaluations", params={"status": "completed"}).json()["total"] == 1
    assert client.get("/api/evaluations", params={"conversation_id": "edge-ru-001"}).json()["total"] == 1
    r = client.post("/api/evaluations", json={"conversation_id": "nope"})
    assert r.status_code == 404
    r = client.post("/api/evaluations", json={"conversation_id": "edge-ru-001", "checklist_version": 7})
    assert r.status_code == 404
    assert client.get("/api/evaluations/ev_nope").status_code == 404


def test_evaluate_failed_is_recorded_not_raised(client):
    _import(client, "EDGE")
    client.provider.script["c03"] = ["это не json"] * 5 + [answer("c03")]   # 3 попытки — все невалидны
    ev = _evaluate(client, "edge-ru-001")
    assert ev["status"] == "failed" and "c03" in ev["status_reason"] and ev["total"] is None


# --- 5. пакеты -------------------------------------------------------------------------------
def _wait_batch(client, batch_id, timeout=10.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        b = client.get(f"/api/batches/{batch_id}").json()
        if b["status"] in ("completed", "cancelled", "failed"):
            return b
        time.sleep(0.05)
    raise AssertionError(f"пакет {batch_id} не завершился: {b}")


def test_batches(client):
    _import(client, "EDGE")
    # ответы без цитат (checked_range #1): валидны для любого диалога, а не только для edge-ru-001
    client.provider.script = {cid: [answer(cid, quote="", checked_range={"from": 1, "to": 1})] for cid in OK}
    r = client.post("/api/batches", json={"group_id": "EDGE", "concurrency": 3})
    assert r.status_code == 202 and r.json()["status"] == "running" and r.json()["total"] == 12
    b = _wait_batch(client, r.json()["batch_id"])
    assert b["status"] == "completed" and b["progress"]["done"] == 12 and b["progress"]["percent"] == 100.0
    assert b["progress"]["completed"] == 11 and b["progress"]["not_evaluable"] == 1 and b["finished_at"]
    assert b["checklist"] == {"id": "chat-quality", "version": 1} and b["cost"]["tokens_in"] > 0
    item = next(i for i in b["items"] if i["conversation_id"] == "edge-ru-001")
    assert item["status"] == "completed" and item["total"] == 93.08 and item["evaluation_id"]
    evs = client.get("/api/evaluations", params={"batch_id": b["batch_id"]}).json()
    assert evs["total"] == 12 and all(e["batch_id"] == b["batch_id"] for e in evs["items"])
    assert client.get("/api/llm-calls", params={"batch_id": b["batch_id"]}).json()["total"] == 88
    # only_not_evaluated: всё уже оценено этой версией → пустой пакет сразу completed
    r = client.post("/api/batches", json={"group_id": "EDGE", "only_not_evaluated": True})
    assert r.status_code == 202 and r.json()["total"] == 0 and r.json()["status"] == "completed"
    # список id + отмена
    r = client.post("/api/batches", json={"conversation_ids": ["edge-ru-001", "edge-ru-002"]})
    bid = r.json()["batch_id"]
    c = client.post(f"/api/batches/{bid}/cancel").json()
    assert c["cancel_requested"] and c["status"] in ("running", "cancelled", "completed")
    b2 = _wait_batch(client, bid)
    assert b2["status"] in ("cancelled", "completed")
    assert client.get("/api/batches").json()["total"] == 3
    assert client.post("/api/batches", json={}).status_code == 400
    assert client.post("/api/batches", json={"conversation_ids": ["nope"]}).status_code == 404
    assert client.get("/api/batches/bt_nope").status_code == 404


# --- 6. результаты ---------------------------------------------------------------------------
def test_results_json_and_csv(client):
    _import(client, "EDGE")
    _evaluate(client, "edge-ru-001")
    _evaluate(client, "edge-ru-005")
    r = client.get("/api/results").json()
    assert r["checklist"]["version"] == 1 and len(r["checklist"]["criteria"]) == 8 and r["total"] == 2
    row = next(i for i in r["items"] if i["conversation_id"] == "edge-ru-001")
    assert row["answers"]["c06"] == 7 and row["final_answers"] == row["answers"] and row["flags"] == []
    assert r["summary"] == {"count": 2, "avg_total": 93.08, "not_evaluable": 1, "failed": 0, "flagged": 0}
    assert client.get("/api/results", params={"language": "KZ"}).json()["total"] == 0
    csv_r = client.get("/api/results", params={"format": "csv"})
    assert csv_r.status_code == 200 and csv_r.headers["content-type"].startswith("text/csv")
    lines = csv_r.text.strip().splitlines()
    assert lines[0].startswith("conversation_id,channel,language,evaluation_id,status,total,final_total,c01") and len(lines) == 3


# --- 7. апелляции ----------------------------------------------------------------------------
def test_appeals_and_decisions(client):
    _import(client, "EDGE")
    ev = _evaluate(client, "edge-ru-001")
    r = client.post(f"/api/evaluations/{ev['id']}/appeals", json={"criterion_id": "c04", "comment": "деталь была",
                                                                  "author": "op"})
    assert r.status_code == 201
    ap = r.json()
    assert ap["status"] == "open" and ap["conversation_id"] == "edge-ru-001" and ap["original"] == {"answer": 20,
                                                                                                    "points": 20.0}
    assert client.post(f"/api/evaluations/{ev['id']}/appeals", json={"criterion_id": "c04", "comment": "ещё"}).status_code == 409
    assert client.post(f"/api/evaluations/{ev['id']}/appeals", json={"criterion_id": "c99", "comment": "x"}).status_code == 404
    assert client.post(f"/api/evaluations/{ev['id']}/appeals", json={"criterion_id": "c05", "comment": " "}).status_code == 422
    assert client.get(f"/api/evaluations/{ev['id']}").json()["evaluation"]["open_appeals"] == 1
    r = client.post(f"/api/appeals/{ap['id']}/decision", json={"decision": "change", "new_answer": 12, "reason": "r"})
    assert r.status_code == 422 and r.json()["error"]["details"]["errors"][0]["path"] == "new_answer"
    r = client.post(f"/api/appeals/{ap['id']}/decision", json={"decision": "change", "new_answer": 15})
    assert r.status_code == 422                                                     # нет причины
    r = client.post(f"/api/appeals/{ap['id']}/decision", json={"decision": "change", "new_answer": 15,
                                                               "reason": "деталь", "reviewer": "qa"})
    assert r.status_code == 201 and r.json()["appeal"]["status"] == "changed"
    fin = r.json()["final"]
    assert fin["total"] == 85.38 and next(c for c in fin["criteria"] if c["criterion_id"] == "c04")["source"] == "appeal"
    assert [e["event"] for e in r.json()["appeal"]["events"]] == ["opened", "in_review", "decided"]
    assert r.json()["appeal"]["decision"]["new_answer"] == 15
    assert client.post(f"/api/appeals/{ap['id']}/decision", json={"decision": "confirm"}).status_code == 409
    got = client.get(f"/api/evaluations/{ev['id']}").json()
    assert got["evaluation"]["total"] == 93.08 and got["evaluation"]["final_total"] == 85.38       # исходная неизменна
    assert got["final"]["total"] == 85.38 and got["appeals"][0]["status"] == "changed"
    res = client.get("/api/results").json()["items"][0]
    assert res["total"] == 93.08 and res["final_total"] == 85.38 and res["final_answers"]["c04"] == 15 and res["answers"]["c04"] == 20
    conv = client.get("/api/conversations", params={"status": "completed"}).json()["items"][0]["latest_evaluation"]
    assert conv["total"] == 93.08 and conv["final_total"] == 85.38
    # review: open → in_review
    ap2 = client.post(f"/api/evaluations/{ev['id']}/appeals", json={"criterion_id": "c06", "comment": "x"}).json()
    r = client.post(f"/api/appeals/{ap2['id']}/review", json={"reviewer": "qa"})
    assert r.status_code == 200 and r.json()["status"] == "in_review"
    assert client.post(f"/api/appeals/{ap2['id']}/review", json={"reviewer": "qa"}).status_code == 409
    assert client.get("/api/appeals", params={"status": "in_review"}).json()["total"] == 1
    assert client.get("/api/appeals", params={"status": "weird"}).status_code == 400
    lst = client.get("/api/appeals", params={"status": "decided"}).json()
    assert lst["total"] == 1 and lst["items"][0]["id"] == ap["id"]
    assert client.get("/api/appeals", params={"status": "open"}).json()["total"] == 0
    assert client.get("/api/appeals", params={"conversation_id": "edge-ru-001", "criterion_id": "c04"}).json()["total"] == 1
    assert client.get(f"/api/appeals/{ap['id']}").json()["status"] == "changed"
    assert client.get("/api/appeals/ap_nope").status_code == 404


# --- 8. логи LLM -----------------------------------------------------------------------------
def test_llm_calls(client):
    _import(client, "EDGE")
    client.provider.script["c01"] = [answer("c01", ans="na"), answer("c01")]       # 1 отклонённый + 1 ok
    ev = _evaluate(client, "edge-ru-001")
    lst = client.get("/api/llm-calls", params={"evaluation_id": ev["id"]}).json()
    assert lst["total"] == 9 and "request" not in lst["items"][0]
    bad = client.get("/api/llm-calls", params={"evaluation_id": ev["id"], "status": "invalid_response"}).json()
    assert bad["total"] == 1 and bad["items"][0]["validation_errors"][0]["code"] == "na_not_allowed"
    one = client.get(f"/api/llm-calls/{lst['items'][0]['id']}").json()
    assert one["request"]["system"] and one["raw_response"] and one["request"]["criterion_id"]
    s = client.get("/api/llm-calls/summary", params={"conversation_id": "edge-ru-001"}).json()
    assert s["calls"] == 9 and s["retries"] == 1 and s["by_model"][0]["model"] == "test-model" and s["cost_usd"] > 0
    assert client.get("/api/llm-calls/llm_nope").status_code == 404
