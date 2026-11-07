"""Конвейер оценки с тестовым провайдером (сеть не используется; это проверка кода, не результаты оценки)."""
import asyncio
import json

import pytest

from backend import db
from backend.evaluator import Evaluator, aggregate_votes
from backend.llm.base import LLMError, LLMProvider, Prices, Usage
from backend.prompt import build_criterion_block, build_dialog_block, build_system, format_transcript, response_schema
from backend.preprocess import preprocess

OK = {"c01": ("yes", 10, "Меня зовут Арман"), "c02": ("na", None, None), "c03": ("na", None, None),
      "c04": (20, 10, "В веб-версии откройте вкладку"), "c05": ("yes", 12, "Пожалуйста"),
      "c06": (10, 10, "Спасибо за ожидание"), "c07": ("yes", 12, "Хорошего дня"), "c08": ("no", None, None)}


class ScriptedProvider(LLMProvider):
    """Отвечает по сценарию: answers[criterion_id] — список ответов на последовательные вызовы."""
    name = "test"

    def __init__(self, script):
        super().__init__("test-model", prices=Prices(input=1.0, output=2.0), concurrency=4)
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []

    async def _call(self, system, user, schema, cache_prefix):
        cid = user.split("КРИТЕРИЙ ", 1)[1].split(":", 1)[0]
        self.calls.append((cid, user))
        item = self.script[cid].pop(0) if len(self.script[cid]) > 1 else self.script[cid][0]
        if isinstance(item, Exception):
            raise item
        text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        return text, Usage(provider=self.name, model=self.model, tokens_in=1000, tokens_out=100)


def answer(cid, ans=None, idx=None, quote=None, **kw):
    a, i, q = OK[cid]
    a = a if ans is None else ans
    i = idx if idx is not None else i
    q = quote if quote is not None else q
    r = {"evidence": [{"idx": i, "quote": q}] if q else [], "checked_range": None if q else {"from": 10, "to": 12},
         "na_reason": "условие выполнено" if a == "na" else None, "reasoning": "обоснование", "answer": a}
    if cid == "c08":
        r["violation_type"] = "none"
    r.update(kw)
    return r


@pytest.fixture
def conn(edge_records, dev_records):
    c = db.connect(":memory:")
    db.init_db(c)
    db.save_dataset(c, "EDGE", "local", list(edge_records.values()))
    db.save_dataset(c, "DEV", "local", [dev_records["p4-ru-002"]])
    yield c
    c.close()


def run(coro):
    return asyncio.run(coro)


def test_full_flow_with_guard_and_cost(conn):
    script = {cid: [answer(cid)] for cid in OK}
    prov = ScriptedProvider(script)
    ev = run(Evaluator(conn, prov).evaluate("edge-ru-001"))
    assert ev["status"] == "completed" and ev["checklist"] == {"id": "chat-quality", "version": 1}
    crit = {c["criterion_id"]: c for c in ev["criteria"]}
    # модель сказала 10, первая реакция 311 с > 180 → guard снизил до 7 и записал это в обоснование
    assert crit["c06"]["answer"] == 7 and crit["c06"]["model_answer"] == 10
    assert "[Правило c06_timing]" in crit["c06"]["explanation"] and "10 → 7" in crit["c06"]["explanation"]
    assert ev["total"] == 93.08                               # совпадает с эталоном edge-ru-001
    assert crit["c01"]["evidence"] == {"kind": "quote", "range": None, "na_condition": None,
                                       "quotes": [{"n": 10, "text": "Меня зовут Арман", "model_quote": "Меня зовут Арман"}]}
    assert crit["c02"]["evidence"]["kind"] == "na_condition" and crit["c02"]["points"] is None
    assert crit["c08"]["violation_type"] == "none" and crit["c08"]["confidence"] == 1.0
    assert len(prov.calls) == 8 and ev["cost"]["calls"] == 8
    assert ev["cost"]["usd"] == pytest.approx(8 * (1000 * 1 + 100 * 2) / 1e6)
    call = db.get_llm_call(conn, ev["llm_call_ids"][0])
    assert call["raw_response"] and call["request"]["system"] and call["model"] == "test-model"


def test_not_evaluable_without_calls(conn):
    prov = ScriptedProvider({})
    ev = run(Evaluator(conn, prov).evaluate("edge-ru-005"))
    assert ev["status"] == "not_evaluable" and ev["total"] is None and ev["criteria"] == []
    assert prov.calls == [] and ev["cost"]["calls"] == 0 and ev["status_reason"] == "Нет реплик оператора"


def test_invalid_then_repaired(conn):
    script = {cid: [answer(cid)] for cid in OK}
    script["c01"] = [answer("c01", ans="na"),                                # N/A не разрешён
                     answer("c01", quote="My name is Arman"),                # цитата не из текста
                     answer("c01")]
    prov = ScriptedProvider(script)
    ev = run(Evaluator(conn, prov).evaluate("edge-ru-001"))
    assert ev["status"] == "completed"
    c01_calls = [u for cid, u in prov.calls if cid == "c01"]
    assert len(c01_calls) == 3
    assert "ОТКЛОНЁН ВАЛИДАТОРОМ" in c01_calls[1] and "не разрешён" in c01_calls[1]
    assert "не найдена в тексте реплики #10" in c01_calls[2]
    statuses = [db.get_llm_call(conn, i)["status"] for i in ev["llm_call_ids"]]
    assert statuses.count("invalid_response") == 2 and ev["cost"]["rejected_calls"] == 2


def test_gives_up_after_two_repairs(conn):
    script = {cid: [answer(cid)] for cid in OK}
    script["c04"] = [answer("c04", ans=12)]                                  # всегда недопустимая ступень
    prov = ScriptedProvider(script)
    ev = run(Evaluator(conn, prov).evaluate("edge-ru-001"))
    assert ev["status"] == "failed" and "c04" in ev["status_reason"] and ev["total"] is None
    assert sum(1 for cid, _ in prov.calls if cid == "c04") == 3


def test_bad_json_is_retried(conn):
    script = {cid: [answer(cid)] for cid in OK}
    script["c05"] = ["это не json", answer("c05")]
    ev = run(Evaluator(conn, ScriptedProvider(script)).evaluate("edge-ru-001"))
    assert ev["status"] == "completed"


def test_provider_error_fails_evaluation_but_is_logged(conn):
    script = {cid: [answer(cid)] for cid in OK}
    script["c03"] = [LLMError("HTTP 503")]
    ev = run(Evaluator(conn, ScriptedProvider(script)).evaluate("edge-ru-001"))
    assert ev["status"] == "failed" and "503" in ev["status_reason"]


def test_votes_majority_and_confidence(conn):
    script = {cid: [answer(cid)] for cid in OK}
    script["c05"] = [answer("c05", ans="yes"), answer("c05", ans="no", evidence=[], checked_range={"from": 10, "to": 12}),
                     answer("c05", ans="yes")]
    script["c04"] = [answer("c04", ans=20), answer("c04", ans=10), answer("c04", ans=15)]
    ev = run(Evaluator(conn, ScriptedProvider(script)).evaluate("edge-ru-001", votes=3))
    crit = {c["criterion_id"]: c for c in ev["criteria"]}
    assert crit["c05"]["answer"] == "yes" and crit["c05"]["confidence"] == pytest.approx(0.667, abs=1e-3)
    assert crit["c04"]["answer"] == 15 and crit["c04"]["confidence"] == pytest.approx(0.333, abs=1e-3)
    assert ev["cost"]["calls"] == 24


@pytest.mark.parametrize("ctype,answers,final,conf", [
    ("binary", ["yes", "no", "yes"], "yes", 0.667),
    ("binary", ["no", "yes"], "no", 0.5),                       # ничья → первый голос
    ("scale", [20, 10, 15], 15, 0.333),
    ("scale", [20, 10], 10, 0.5),                                # median_low — всегда допустимая ступень
    ("scale", ["na", "na", 20], "na", 0.667),
    ("scale", ["na", 20, 15], 15, 0.333),
    ("flag", ["no", "no", "no"], "no", 1.0),
])
def test_aggregate_votes(ctype, answers, final, conf):
    assert aggregate_votes({"type": ctype}, answers) == (final, pytest.approx(conf, abs=1e-3))


# --- промпт -------------------------------------------------------------------------------
def test_prompt_built_from_config(edge_records, dev_records, checklist_v1):
    sys_chat = build_system("agent chat")
    assert "R7." in sys_chat and "R11." in sys_chat and "Правила для звонков" not in sys_chat
    assert "ровно в одном критерии" in sys_chat
    assert "Ошибки распознавания не считаются ошибками" in build_system("voice call")

    pre = preprocess(edge_records["edge-ru-002"], checklist_v1["settings"])
    dialog = build_dialog_block(pre, checklist_v1["settings"])
    assert "Первая реакция оператора: 47 с (порог 180 с)" in dialog
    assert "#11 → #13: 634 с; существенная; БЕЗ предупреждения" in dialog
    assert "#1: Мне нужен отчёт брокера" in dialog                    # клиент до подключения
    assert "[#10 | оператор | 01:20 | ] Здравствуйте, меня зовут Дарина." in dialog

    call = preprocess(dev_records["p4-ru-002"], checklist_v1["settings"])
    tr = format_transcript(call)
    assert "| клиент_не_слышит] Добрый день. Клиент желает купить" in tr and "| после_завершения]" in tr
    assert "Удержание: #18–#59" in build_dialog_block(call, checklist_v1["settings"])

    crit = {c["id"]: c for c in checklist_v1["criteria"]}
    block = build_criterion_block(crit["c04"])
    assert "Корректная и полная консультация" in block and "15 — Основной вопрос решён" in block
    assert "Чат закончился до того" in block
    assert "N/A НЕ разрешён" in build_criterion_block(crit["c01"])
    others = [c for c in checklist_v1["criteria"] if c["id"] != "c06"]
    b06 = build_criterion_block(crit["c06"], others=others)
    assert "ДРУГИЕ КРИТЕРИИ ЧЕК-ЛИСТА" in b06 and "Корректная и полная консультация:" in b06
    assert "Управление диалогом, скорость и грамотность:" not in b06
    new = {"id": "c99", "title": "Новый критерий", "type": "scale", "weight": 3, "na_allowed": False,
           "description": "Описание нового", "scale": [{"value": 0, "description": "плохо"}, {"value": 3, "description": "хорошо"}]}
    assert "КРИТЕРИЙ c99: Новый критерий" in build_criterion_block(new) and "3 — хорошо" in build_criterion_block(new)
    assert response_schema(new)["properties"]["answer"] == {"anyOf": [{"type": "integer", "enum": [0, 3]}]}


def test_schema_order_and_flag(checklist_v1):
    crit = {c["id"]: c for c in checklist_v1["criteria"]}
    s = response_schema(crit["c08"])
    assert list(s["properties"]) == ["evidence", "checked_range", "na_reason", "reasoning", "answer", "violation_type"]
    assert s["properties"]["answer"]["enum"] == ["yes", "no"]
    assert response_schema(crit["c02"])["properties"]["answer"]["enum"] == ["yes", "no", "na"]
    c04 = response_schema(crit["c04"])["properties"]["answer"]["anyOf"]
    assert c04 == [{"type": "integer", "enum": [0, 5, 10, 15, 20]}, {"type": "string", "enum": ["na"]}]


def test_flag_hints_in_prompt(dev_records, checklist_v1):
    from backend.hints import find_hints
    c08 = next(c for c in checklist_v1["criteria"] if c["id"] == "c08")
    pre = preprocess(dev_records["p1-ru-022"], checklist_v1["settings"])
    hints = find_hints(c08, pre)
    assert {h["name"] for h in hints} >= {"transfer_to_me"}
    block = build_criterion_block(c08, hints)
    assert "ПОДСКАЗКИ" in block and "#10:" in block
    # подсказки ищутся только в засчитываемых репликах оператора
    assert find_hints(c08, preprocess(dev_records["p1-ru-001"])) == []


@pytest.mark.parametrize("text,name", [
    ("Скиньте деньги мне на карту", "transfer_to_me"),
    ("Переведите мне, я вложу", "transfer_to_me"),
    ("Маған ақшаны аударыңыз", "transfer_to_me"),
    ("Just send the money to me", "transfer_to_me"),
    ("Вот мой личный номер, пишите", "personal_number"),
    ("Менің жеке нөмірім", "personal_number"),
    ("Text me on WhatsApp, my personal number", "personal_number"),
    ("Могу дать платную консультацию", "paid_consultation"),
    ("Ақылы кеңес беремін", "paid_consultation"),
    ("I offer paid consultation", "paid_consultation"),
    ("Переведите на мою карту", "personal_card"),
    ("Через компанию оформлять не надо", "bypass_company"),
])
def test_hint_patterns(text, name, checklist_v1):
    from backend.hints import find_hints
    c08 = next(c for c in checklist_v1["criteria"] if c["id"] == "c08")
    rec = {"conversation_id": "t", "channel": "agent chat", "transcript": [
        {"timestamp": "00:00", "speaker": "system", "text": "Эскалация на оператора."},
        {"timestamp": "00:10", "speaker": "operator", "text": text}]}
    assert name in {h["name"] for h in find_hints(c08, preprocess(rec))}


def test_auth_error_aborts(conn):
    script = {cid: [LLMError("нет учётных данных", code="auth")] for cid in OK}
    with pytest.raises(LLMError) as e:
        run(Evaluator(conn, ScriptedProvider(script)).evaluate("edge-ru-001"))
    assert e.value.code == "auth"


def test_failure_reason_deduplicated(conn):
    script = {cid: [LLMError("HTTP 503")] for cid in OK}
    ev = run(Evaluator(conn, ScriptedProvider(script)).evaluate("edge-ru-001"))
    assert ev["status_reason"].count("HTTP 503") == 1 and "c01, c02" in ev["status_reason"]


def test_settings_defaults_fill_missing_keys(conn):
    """B2: версия чек-листа без pause_threshold_s — guard c06 работает с умолчанием (= first_response_threshold_s)."""
    v1 = db.get_checklist(conn, "chat-quality", 1)
    body = {k: v1[k] for k in ("id", "name", "sections", "criteria")}
    body["settings"] = {"first_response_threshold_s": 180}
    v2 = db.insert_checklist_version(conn, body, base_version=1, author="qa", comment="без pause_threshold_s")
    script = {cid: [answer(cid)] for cid in OK}
    ev = run(Evaluator(conn, ScriptedProvider(script)).evaluate("edge-ru-001", checklist_version=v2["version"]))
    assert ev["status"] == "completed", ev["status_reason"]
    c06 = next(c for c in ev["criteria"] if c["criterion_id"] == "c06")
    assert c06["answer"] == 7 and "first_response_s = 311 > 180" in c06["guards"][0]["conditions"]
    assert ev["facts"]["pause_threshold_s"] == 180


def test_any_exception_becomes_failed_status(conn, monkeypatch):
    """B2: сбой предобработки или кода оценки → статус failed с текстом ошибки, без исключения наружу."""
    db.save_dataset(conn, "BAD", "local", [{"conversation_id": "mixed", "channel": "agent chat", "language": "RU",
                                            "transcript": [{"timestamp": "00:00", "speaker": "client", "text": "a"},
                                                           {"timestamp": "2026-11-05T10:00:00+00:00", "speaker": "operator",
                                                            "text": "b"}]}])
    prov = ScriptedProvider({})
    ev = run(Evaluator(conn, prov).evaluate("mixed"))
    assert ev["status"] == "failed" and "смешаны форматы времени" in ev["status_reason"] and prov.calls == []
    assert ev["total"] is None and ev["criteria"] == []

    def boom(*a, **kw):
        raise RuntimeError("guard сломан")
    monkeypatch.setattr("backend.evaluator.apply_guards", boom)
    script = {cid: [answer(cid)] for cid in OK}
    ev = run(Evaluator(conn, ScriptedProvider(script)).evaluate("edge-ru-001"))
    assert ev["status"] == "failed" and ev["status_reason"] == "RuntimeError: guard сломан"
    assert ev["facts"]["operator_messages"] > 0           # факты сохранены, LLM-вызовы залогированы
    assert ev["cost"]["calls"] == 8


def test_stored_quote_is_original_substring(conn):
    """B3: evidence.quotes[].text — всегда подстрока исходного text реплики; форма модели — в model_quote."""
    script = {cid: [answer(cid)] for cid in OK}
    script["c01"] = [answer("c01", quote="меня зовут Арманн")]
    script["c05"] = [answer("c05", quote="пожалуйста!")]
    ev = run(Evaluator(conn, ScriptedProvider(script)).evaluate("edge-ru-001"))
    assert ev["status"] == "completed"
    texts = {m.idx: m.text for m in preprocess(db.get_conversation(conn, "edge-ru-001")).messages}
    quotes = [q for c in ev["criteria"] for q in c["evidence"].get("quotes", [])]
    assert quotes and all(q["text"] in texts[q["n"]] for q in quotes)
    c01 = next(c for c in ev["criteria"] if c["criterion_id"] == "c01")
    assert c01["evidence"]["quotes"] == [{"n": 10, "text": "Меня зовут Арман", "model_quote": "меня зовут Арманн"}]
