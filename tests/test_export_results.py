"""Формат сдачи организаторам: экспорт (коды, значения, evidence_turns по исходному массиву, статусы, модель)
и валидатор файла (намеренно испорченные документы). Сеть не используется."""
import asyncio
import copy
import json

import pytest

from backend import db
from backend.evaluator import Evaluator
from backend.export_results import (export_group, from_org_value, load_mapping, short_rationale, to_org_value,
                                    transcript_lengths, validate_document, validate_file, write_document)
from tests.test_evaluator import OK, ScriptedProvider, answer

MAPPING = load_mapping()
CODES = [m["code"] for m in MAPPING["criteria"]]

# p4-ru-001 (звонок с точным дублем в позиции 21): цитаты из реплик ПОСЛЕ дубля, по исходной нумерации
P4 = {"c01": answer("c01", idx=24, quote="Да."),
      "c02": answer("c02", ans="na", quote="", checked_range={"from": 24, "to": 31}),
      "c03": answer("c03", ans="na", quote="", checked_range={"from": 24, "to": 26}),
      "c04": answer("c04", ans=10, idx=26, quote="вы сейчас обращаетесь в техническую поддержку клиентов"),
      "c05": answer("c05", ans="no", quote="", checked_range={"from": 24, "to": 31}),
      "c06": answer("c06", ans=7, idx=27, quote="Если вас не хватает активов для закрепления"),
      "c07": answer("c07", ans="no", quote="", checked_range={"from": 30, "to": 31}),
      "c08": answer("c08", quote="", checked_range={"from": 24, "to": 31})}


@pytest.fixture
def conn(edge_records, dev_records):
    c = db.connect(":memory:")
    db.init_db(c)
    db.save_dataset(c, "EDGE", "local", list(edge_records.values()))
    db.save_dataset(c, "DEV", "local", [dev_records["p4-ru-001"], dev_records["p1-ru-001"]])
    yield c
    c.close()


def evaluate(conn, cid, script):
    return asyncio.run(Evaluator(conn, ScriptedProvider({k: [v] for k, v in script.items()})).evaluate(cid))


def test_value_mapping_roundtrip():
    assert to_org_value("yes", MAPPING) is True and to_org_value("no", MAPPING) is False
    assert to_org_value("na", MAPPING) is None and to_org_value(15, MAPPING) == 15
    for v in (True, False, None, 0, 7, 20):
        assert to_org_value(from_org_value(v, MAPPING), MAPPING) is v or to_org_value(from_org_value(v, MAPPING), MAPPING) == v
    with pytest.raises(ValueError):
        to_org_value("maybe", MAPPING)
    assert short_rationale("Первое. Второе. Третье. [Правило c06_timing] 10 → 7.") == "Первое. Второе. [Правило c06_timing] 10 → 7."
    assert short_rationale("") == "Обоснование отсутствует."


def test_evidence_turns_point_to_original_transcript_call_with_duplicate(conn, dev_records):
    """Звонок p4-ru-001: дубль в позиции 21 не удаляется, номера доказательств = позиции исходного массива."""
    rec = dev_records["p4-ru-001"]
    assert rec["transcript"][20]["text"] == rec["transcript"][19]["text"]          # дубль есть
    ev = evaluate(conn, "p4-ru-001", P4)
    assert ev["status"] == "completed"
    doc = export_group(conn, "DEV", team_name="Тест")
    rec_out = next(r for r in doc["results"] if r["conversation_id"] == "p4-ru-001")
    assert rec_out["status"] == "ok" and [c["criterion_id"] for c in rec_out["criteria"]] == CODES
    by = {c["criterion_id"]: c for c in rec_out["criteria"]}
    # цитаты → ровно те реплики, и текст исходной реплики (1-based) содержит цитату
    for code, cid in (("contact", "c01"), ("consultation", "c04"), ("dialogue", "c06")):
        q = P4[cid]["evidence"][0]
        assert by[code]["evidence_turns"] == [q["idx"]]
        assert q["quote"] in rec["transcript"][q["idx"] - 1]["text"]
    assert rec["transcript"][25]["text"].startswith("Нет, вы сейчас обращаетесь")   # позиция 26 исходного массива
    # пропущенное действие и N/A → проверенный диапазон
    assert by["empathy"]["evidence_turns"] == list(range(24, 32)) and by["closure"]["evidence_turns"] == [30, 31]
    assert by["discovery"]["evidence_turns"] == list(range(24, 32)) and by["identification"]["evidence_turns"] == [24, 25, 26]
    assert by["contact"]["value"] is True and by["discovery"]["value"] is None and by["identification"]["value"] is None
    assert by["consultation"]["value"] == 10 and by["empathy"]["value"] is False and by["closure"]["value"] is False
    assert by["critical"]["value"] is False and by["dialogue"]["value"] in (0, 4, 7, 10)
    assert rec_out["score"]["critical_flag"] is False and rec_out["score"]["final_score"] == ev["total"]
    assert all(c["rationale"] for c in rec_out["criteria"])
    # запись без оценки — error, не пропуск и не нули
    other = next(r for r in doc["results"] if r["conversation_id"] == "p1-ru-001")
    assert other == {"conversation_id": "p1-ru-001", "status": "error", "criteria": [], "score": None,
                     "error": "Оценка не выполнена"}


def test_evidence_turns_chat_and_statuses(conn, edge_records):
    ev = evaluate(conn, "edge-ru-001", {cid: answer(cid) for cid in OK})
    assert ev["status"] == "completed" and ev["total"] == 93.08
    bad = {cid: answer(cid) for cid in OK}
    bad["c05"] = answer("c05", ans="maybe")                                   # всегда невалидно → failed
    assert evaluate(conn, "edge-ru-002", bad)["status"] == "failed"
    assert evaluate(conn, "edge-ru-005", {cid: answer(cid) for cid in OK})["status"] == "not_evaluable"
    doc = export_group(conn, "EDGE", team_name="Тест")
    res = {r["conversation_id"]: r for r in doc["results"]}
    assert len(doc["results"]) == len(edge_records) and set(res) == set(edge_records)
    ok = res["edge-ru-001"]
    by = {c["criterion_id"]: c for c in ok["criteria"]}
    tr = edge_records["edge-ru-001"]["transcript"]
    assert by["contact"]["evidence_turns"] == [10] and "Меня зовут Арман" in tr[9]["text"]
    assert by["closure"]["evidence_turns"] == [12] and "Хорошего дня" in tr[11]["text"]
    assert by["discovery"]["value"] is None and by["discovery"]["evidence_turns"] == [10, 11, 12]   # N/A → checked_range
    assert by["critical"]["evidence_turns"] == [10, 11, 12] and by["dialogue"]["value"] == 7           # guard учтён
    assert ok["score"] == {"final_score": 93.08, "critical_flag": False}
    assert res["edge-ru-002"]["status"] == "error" and "c05" in res["edge-ru-002"]["error"]
    assert res["edge-ru-005"] == {"conversation_id": "edge-ru-005", "status": "error", "criteria": [], "score": None,
                                  "error": "В диалоге нет реплик оператора"}
    assert doc["model"] == {"mode": "external_api", "name": "test-model"} and doc["team_name"] == "Тест"
    # модель: вызовы к локальному серверу + внешние → hybrid, оба имени
    db.insert_llm_call(conn, {"evaluation_id": ev["id"], "conversation_id": "edge-ru-001", "provider": "openai",
                              "model": "cqa-qwen14b", "status": "ok",
                              "request": {"base_url": "http://localhost:11434/v1", "model": "cqa-qwen14b"}})
    assert export_group(conn, "EDGE", team_name="Тест")["model"] == {"mode": "hybrid", "name": "cqa-qwen14b; test-model"}


def test_export_rejects_criterion_without_code(conn, checklist_v1):
    cl = json.loads(json.dumps(checklist_v1))
    cl["criteria"].append({"id": "c09", "section": "conduct", "title": "Новый", "type": "binary", "weight": 1,
                           "na_allowed": False, "description": "x"})
    cl["version"] = 2
    db.insert_checklist_version(conn, cl, author="t", version=2)
    with pytest.raises(ValueError, match="c09"):
        export_group(conn, "EDGE", checklist_version=2, team_name="Тест")
    with pytest.raises(db.NotFound):
        export_group(conn, "NOPE", team_name="Тест")


@pytest.fixture
def valid_doc(conn, edge_records, checklist_v1):
    evaluate(conn, "edge-ru-001", {cid: answer(cid) for cid in OK})
    evaluate(conn, "edge-ru-005", {cid: answer(cid) for cid in OK})
    doc = export_group(conn, "EDGE", team_name="Тест")
    lengths = transcript_lengths(list(edge_records.values()))
    assert validate_document(doc, lengths=lengths, checklist=checklist_v1) == []
    return doc, lengths


def _errors(doc, lengths, checklist_v1, **kw):
    return validate_document(doc, lengths=lengths, checklist=checklist_v1, **kw)


def test_validator_catches_each_corruption(valid_doc, checklist_v1):
    doc, lengths = valid_doc
    ok_i = next(i for i, r in enumerate(doc["results"]) if r["status"] == "ok")
    err_i = next(i for i, r in enumerate(doc["results"]) if r["status"] == "error")

    def corrupt(fn):
        d = copy.deepcopy(doc)
        fn(d)
        return _errors(d, lengths, checklist_v1)

    cases = {
        "team placeholder": lambda d: d.update(team_name="[НАЗВАНИЕ КОМАНДЫ]"),
        "schema_version": lambda d: d.update(schema_version="2.0"),
        "extra root key": lambda d: d.update(comment="x"),
        "mode": lambda d: d["model"].update(mode="cloud"),
        "model name empty": lambda d: d["model"].update(name=""),
        "duplicate id": lambda d: d["results"].append(copy.deepcopy(d["results"][ok_i])),
        "missing record": lambda d: d["results"].pop(err_i),
        "unknown id": lambda d: d["results"][err_i].update(conversation_id="zzz"),
        "criterion missing": lambda d: d["results"][ok_i]["criteria"].pop(0),
        "criterion twice": lambda d: d["results"][ok_i]["criteria"].append(copy.deepcopy(d["results"][ok_i]["criteria"][0])),
        "unknown code": lambda d: d["results"][ok_i]["criteria"][0].update(criterion_id="politeness"),
        "value out of scale": lambda d: d["results"][ok_i]["criteria"][3].update(value=3),
        "null where not allowed": lambda d: d["results"][ok_i]["criteria"][0].update(value=None),
        "bool as int": lambda d: d["results"][ok_i]["criteria"][0].update(value=1),
        "rationale empty": lambda d: d["results"][ok_i]["criteria"][0].update(rationale=" "),
        "evidence empty": lambda d: d["results"][ok_i]["criteria"][0].update(evidence_turns=[]),
        "evidence out of range": lambda d: d["results"][ok_i]["criteria"][0].update(evidence_turns=[13]),
        "evidence zero": lambda d: d["results"][ok_i]["criteria"][0].update(evidence_turns=[0]),
        "evidence not int": lambda d: d["results"][ok_i]["criteria"][0].update(evidence_turns=["10"]),
        "criterion extra key": lambda d: d["results"][ok_i]["criteria"][0].update(points=10),
        "final_score wrong": lambda d: d["results"][ok_i]["score"].update(final_score=50.0),
        "final_score 3 decimals": lambda d: d["results"][ok_i]["score"].update(final_score=93.077),
        "final_score string": lambda d: d["results"][ok_i]["score"].update(final_score="93.08"),
        "critical_flag mismatch": lambda d: d["results"][ok_i]["score"].update(critical_flag=True),
        "critical_flag not bool": lambda d: d["results"][ok_i]["score"].update(critical_flag=0),
        "score null for ok": lambda d: d["results"][ok_i].update(score=None),
        "status unknown": lambda d: d["results"][ok_i].update(status="done"),
        "ok with error key": lambda d: d["results"][ok_i].update(error="x"),
        "error with criteria": lambda d: d["results"][err_i].update(criteria=[{"criterion_id": "contact"}]),
        "error with score": lambda d: d["results"][err_i].update(score={"final_score": 0, "critical_flag": False}),
        "error text empty": lambda d: d["results"][err_i].update(error=""),
        "error zeros instead": lambda d: d["results"][err_i].update(status="ok", score={"final_score": 0.0, "critical_flag": False}),
    }
    for name, fn in cases.items():
        assert corrupt(fn), f"испорченный документ принят: {name}"
    assert _errors(doc, lengths, checklist_v1, expected_count=10) and not _errors(doc, lengths, checklist_v1, expected_count=12)
    assert _errors([], lengths, checklist_v1) == ["корень файла — не JSON-объект"]


def test_validate_file_utf8_and_json(valid_doc, checklist_v1, tmp_path):
    doc, lengths = valid_doc
    path = write_document(doc, tmp_path / "out")
    assert path.name == "results.json" and validate_file(path, lengths=lengths, checklist=checklist_v1) == []
    assert json.loads(path.read_text(encoding="utf-8")) == doc and "\\u" not in path.read_text(encoding="utf-8")
    (tmp_path / "bad.json").write_bytes(b'{"schema_version": "1.0", "team_name": "\xff\xfe"}')
    assert validate_file(tmp_path / "bad.json", lengths=lengths, checklist=checklist_v1)[0].startswith("файл не в UTF-8")
    (tmp_path / "bad2.json").write_text("{\"schema_version\": ", encoding="utf-8")
    assert "JSON" in validate_file(tmp_path / "bad2.json", lengths=lengths, checklist=checklist_v1)[0]
