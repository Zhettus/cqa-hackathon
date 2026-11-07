"""Поле reference критерия (справочник компании): валидация, промпт, посев версий, сборка из knowledge.md."""
import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from backend import db
from backend.checklist import load_default_checklist, validate_checklist
from backend.evaluator import Evaluator
from backend.prompt import REFERENCE_RULE, build_criterion_block
from tests.conftest import ROOT
from tests.test_evaluator import OK, ScriptedProvider, answer

REF = "1. Форма 1042-S предоставляется на английском языке.\n2. Тариф виден в «Кабинете» под ФИО."


def _with_reference(cid="c04", text=REF) -> dict:
    cl = json.loads(json.dumps(load_default_checklist()))
    next(c for c in cl["criteria"] if c["id"] == cid)["reference"] = text
    return cl


def test_reference_validation():
    assert validate_checklist(_with_reference()) == []
    bad = _with_reference(text=["не", "строка"])
    assert any(e["path"].endswith(".reference") for e in validate_checklist(bad))
    long = _with_reference(text="x" * 20001)
    assert any(e["path"].endswith(".reference") for e in validate_checklist(long))
    assert validate_checklist(_with_reference("c01", "любой критерий")) == []        # поле не привязано к c04


def test_prompt_block_with_and_without_reference():
    cl = _with_reference()
    c04 = next(c for c in cl["criteria"] if c["id"] == "c04")
    block = build_criterion_block(c04)
    assert "СПРАВОЧНЫЕ ФАКТЫ КОМПАНИИ" in block and REF in block and REFERENCE_RULE.strip() in block
    assert "противоречит им — это ошибочная рекомендация" in block and "не является основанием снижать балл" in block
    c01 = next(c for c in cl["criteria"] if c["id"] == "c01")
    assert "СПРАВОЧНЫЕ ФАКТЫ" not in build_criterion_block(c01)


def test_reference_reaches_model_only_for_that_criterion(edge_records):
    conn = db.connect(":memory:")
    db.init_db(conn)
    db.save_dataset(conn, "EDGE", "local", list(edge_records.values()))
    v1 = db.get_checklist(conn, "chat-quality", 1)
    body = {k: v1[k] for k in ("id", "name", "sections", "criteria", "settings")}
    next(c for c in body["criteria"] if c["id"] == "c04")["reference"] = REF
    v2 = db.insert_checklist_version(conn, body, base_version=1, author="t", comment="reference")
    assert next(c for c in v2["criteria"] if c["id"] == "c04")["reference"] == REF
    prov = ScriptedProvider({cid: [answer(cid)] for cid in OK})
    ev = asyncio.run(Evaluator(conn, prov).evaluate("edge-ru-001", checklist_version=v2["version"]))
    assert ev["status"] == "completed" and ev["checklist"]["version"] == v2["version"]
    users = {cid: user for cid, user in prov.calls}
    assert "СПРАВОЧНЫЕ ФАКТЫ КОМПАНИИ" in users["c04"] and REF in users["c04"]
    assert all("СПРАВОЧНЫЕ ФАКТЫ" not in u for cid, u in users.items() if cid != "c04")
    # v1 без reference — блока нет
    prov2 = ScriptedProvider({cid: [answer(cid)] for cid in OK})
    asyncio.run(Evaluator(conn, prov2).evaluate("edge-ru-001", checklist_version=1))
    assert all("СПРАВОЧНЫЕ ФАКТЫ" not in u for _, u in prov2.calls)


def test_knowledge_md_to_reference_strips_sources():
    spec = importlib.util.spec_from_file_location("build_ref", ROOT / "scripts" / "build_reference_version.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    text = mod.reference_text(ROOT / "docs" / "knowledge.md")
    lines = text.splitlines()
    assert 15 <= len(lines) <= 25
    assert "источник" not in text and "p1-" not in text and "p4-" not in text and "edge-" not in text
    assert all(line.split(". ", 1)[0].isdigit() for line in lines)


def test_init_db_seeds_all_versions(tmp_path, monkeypatch):
    import backend.checklist as chk
    v1 = load_default_checklist()
    v2 = _with_reference()
    v2["version"] = 2
    (tmp_path / "default_v1.json").write_text(json.dumps(v1, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "default_v2.json").write_text(json.dumps(v2, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(chk, "CHECKLISTS_DIR", tmp_path)
    conn = db.connect(":memory:")
    db.init_db(conn)
    assert [v["version"] for v in db.list_checklist_versions(conn, "chat-quality")] == [1, 2]
    assert next(c for c in db.get_checklist(conn, "chat-quality")["criteria"] if c["id"] == "c04")["reference"] == REF
    db.init_db(conn)                                                    # повторный старт ничего не дублирует
    assert [v["version"] for v in db.list_checklist_versions(conn, "chat-quality")] == [1, 2]
