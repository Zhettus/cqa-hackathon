import json

import pytest

from backend.preprocess import (AFTER_END, CLIENT_NOT_HEARING, DTMF, DUPLICATE, IVR, detect_language,
                                parse_timestamp, preprocess)
from tests.conftest import ROOT

SETTINGS = {"first_response_threshold_s": 180}
# Факты эталона, которые код не вычисляет (нужна семантика — решает модель).
NOT_COMPUTED = {"first_disclosure_at"}
# Факты, сверяемые с исходной записью, а не с preprocess.
RECORD_FIELDS = {"audio_url"}


def _edge_fact_cases():
    for a in json.loads((ROOT / "eval" / "edge_cases.json").read_text(encoding="utf-8"))["annotations"]:
        for k, v in (a.get("facts") or {}).items():
            yield pytest.param(a["conversation_id"], k, v, id=f"{a['conversation_id']}:{k}")


@pytest.mark.parametrize("cid,key,expected", list(_edge_fact_cases()))
def test_edge_facts(cid, key, expected, edge_records):
    rec = edge_records[cid]
    if key in NOT_COMPUTED:
        pytest.skip(f"{key} не вычисляется кодом")
    if key in RECORD_FIELDS:
        assert rec.get(key) == expected
        return
    facts = preprocess(rec, SETTINGS).facts.model_dump()
    assert key in facts, f"preprocess не отдаёт факт {key}"
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        assert facts[key] == pytest.approx(expected, abs=1), (cid, key)
    else:
        assert facts[key] == expected, (cid, key)


def test_language_matches_labels(dev_records, edge_records):
    wrong = [(cid, r["language"], preprocess(r).facts.client_language)
             for cid, r in {**dev_records, **edge_records}.items()
             if preprocess(r).facts.client_language != r["language"]]
    assert wrong == []


@pytest.mark.parametrize("texts,lang", [
    (["Где посмотреть тариф?"], "RU"),
    (["Сәлеметсіз бе, маған брокер отчёт керек"], "KZ"),
    (["Получилось, рақмет!"], "KZ"),
    (["Where do I find my tariff? Freedom Broker app"], "EN"),
    (["Здравствуйте, Freedom Broker, меня зовут Арман"], "RU"),
    (["[Данные идентификации скрыты]"], None),
    (["2", "#"], None),
])
def test_detect_language(texts, lang):
    assert detect_language(texts) == lang


def test_p4_ru_002_hold_and_consultation(dev_records):
    p = preprocess(dev_records["p4-ru-002"], SETTINGS)
    by = {m.idx: m for m in p.messages}
    hold_start = next(m.idx for m in p.messages if m.event == "hold_start")
    hold_end = next(m.idx for m in p.messages if m.event == "hold_end")
    on_hold_ops = [m for m in p.messages if hold_start < m.idx < hold_end and m.speaker == "operator"]
    assert on_hold_ops, "на удержании должны быть реплики оператора/коллеги"
    assert all(CLIENT_NOT_HEARING in m.flags and not m.counts_for_operator for m in on_hold_ops)
    # реплика коллеги-трейдера во время консультации
    assert "Клиент желает купить" in by[21].text and CLIENT_NOT_HEARING in by[21].flags
    assert by[21].segment == "consultation"
    # после снятия с удержания оператор снова говорит с клиентом
    assert by[60].counts_for_operator and by[60].segment == "operator"
    assert p.facts.hold_s == pytest.approx(425.9, abs=0.2)
    assert [(i.from_idx, i.to_idx) for i in p.facts.consultation_intervals] == [(16, 47)]
    # слова после «Клиент завершил разговор»
    assert AFTER_END in by[73].flags and by[73].segment == "after_end"
    # словесная идентификация: «Да, всё верно» после скрытых ФИО и адреса
    assert p.facts.identity_verified_idx == 15 and p.facts.identity_source == "verbal"
    assert p.facts.closing_event == "client_ended_call"


def test_p4_ru_001_duplicate_removed(dev_records):
    p = preprocess(dev_records["p4-ru-001"], SETTINGS)
    assert p.facts.duplicates == [21]
    m20, m21 = p.messages[19], p.messages[20]
    assert (m20.timestamp, m20.text) == (m21.timestamp, m21.text)
    assert DUPLICATE in m21.flags and DUPLICATE not in m20.flags
    visible = p.visible_messages()
    assert 21 not in [m.idx for m in visible] and 20 in [m.idx for m in visible]
    assert len(visible) == len(p.messages) - 1
    # исходник не тронут, нумерация исходная
    assert p.messages[20].idx == 21 and len(p.messages) == len(dev_records["p4-ru-001"]["transcript"])


def test_p4_ru_001_ivr_dtmf_and_audio(dev_records):
    p = preprocess(dev_records["p4-ru-001"], SETTINGS)
    by = {m.idx: m for m in p.messages}
    joined = next(m.idx for m in p.messages if m.event == "agent_joined")
    assert all(IVR in m.flags for m in p.messages if m.idx < joined and m.speaker != "system")
    assert DTMF in by[4].flags and DTMF in by[7].flags          # «2», «0»
    assert by[13].speaker == "operator" and by[13].counts_for_operator and IVR not in by[13].flags
    # пауза записи «приостановлены» (10) → «возобновлены» (12) вычитается
    pause = by[12].t_s - by[10].t_s
    assert by[13].audio_offset_s == pytest.approx(by[13].t_s - pause, abs=0.15)
    assert by[3].audio_offset_s == pytest.approx(by[3].t_s, abs=0.15)


def test_edge_kz_003_call(edge_records):
    p = preprocess(edge_records["edge-kz-003"], SETTINGS)
    f = p.facts
    assert f.identity_source == "verbal" and f.identity_verified_at.startswith("2026-11-05T10:01:36")
    assert f.client_language == "KZ" and f.operator_first_reply_language == "KZ"
    assert DTMF in p.messages[2].flags and IVR in p.messages[2].flags


def test_chat_facts(edge_records, dev_records):
    f = preprocess(edge_records["edge-ru-004"], SETTINGS).facts
    assert f.identity_source == "system" and f.identity_verified_idx == 13
    assert f.escalation_at == "00:50" and f.first_response_s == 45
    assert preprocess(edge_records["edge-ru-005"]).facts.closing_event == "client_left"
    assert preprocess(dev_records["p1-ru-012"]).facts.closing_event == "connection_lost"
    assert preprocess(dev_records["p1-en-013"]).facts.closing_event == "operator_closed"
    assert preprocess(dev_records["p1-ru-001"]).facts.closing_event == "none"
    # «Спасибо за ожидание» после паузы — не предупреждение
    g = preprocess(edge_records["edge-ru-001"], SETTINGS).facts.gaps[0]
    assert g.kind == "first_response" and not g.warned and g.significant


def test_both_time_formats():
    assert parse_timestamp("05:52") == ("mmss", 352.0)
    assert parse_timestamp("1:02:03") == ("mmss", 3723.0)
    kind, a = parse_timestamp("2026-11-03T15:49:13.1900670+00:00")
    _, b = parse_timestamp("2026-11-03T15:49:22.4171535+00:00")
    assert kind == "iso" and b - a == pytest.approx(9.2270865, abs=1e-5)
    assert parse_timestamp("2026-11-05T10:00:06Z")[0] == "iso"
    with pytest.raises(ValueError):
        parse_timestamp("вчера")

    chat = {"conversation_id": "x", "channel": "agent chat", "transcript": [
        {"timestamp": "00:00", "speaker": "client", "text": "Привет"},
        {"timestamp": "01:30", "speaker": "operator", "text": "Здравствуйте"}]}
    call = {"conversation_id": "y", "channel": "voice call", "transcript": [
        {"timestamp": "2026-11-05T10:00:00+00:00", "speaker": "client", "text": "Алло"},
        {"timestamp": "2026-11-05T10:01:30.5+00:00", "speaker": "operator", "text": "Здравствуйте"}]}
    assert preprocess(chat).messages[1].t_s == 90.0 and preprocess(chat).messages[1].audio_offset_s is None
    assert preprocess(call).messages[1].t_s == 90.5 and preprocess(call).messages[1].audio_offset_s == 90.5


def test_mixed_time_formats_rejected():
    rec = {"conversation_id": "z", "channel": "agent chat", "transcript": [
        {"timestamp": "00:00", "speaker": "client", "text": "a"},
        {"timestamp": "2026-11-05T10:00:00+00:00", "speaker": "operator", "text": "b"}]}
    with pytest.raises(ValueError):
        preprocess(rec)


def test_empty_transcript():
    p = preprocess({"conversation_id": "e", "channel": "agent chat", "language": "RU", "transcript": []})
    assert p.messages == [] and p.facts.operator_messages == 0
    assert p.facts.first_response_s is None and p.facts.client_language is None
    assert p.facts.closing_event == "none" and p.facts.max_unwarned_gap_s == 0


def test_no_operator(edge_records):
    p = preprocess(edge_records["edge-ru-005"], SETTINGS)
    assert p.facts.operator_messages == 0 and p.facts.first_operator_idx is None
    assert p.facts.first_response_s is None and p.facts.gaps == []
    assert p.facts.escalation_at == "00:38"
    assert not any(m.counts_for_operator for m in p.messages)


def test_only_operator_counts(dev_records, edge_records):
    for r in {**dev_records, **edge_records}.values():
        for m in preprocess(r).messages:
            if m.counts_for_operator:
                assert m.speaker == "operator" and m.segment == "operator"


def test_source_fields_untouched(dev_records):
    rec = dev_records["p4-ru-003"]
    before = json.dumps(rec, ensure_ascii=False)
    p = preprocess(rec)
    assert json.dumps(rec, ensure_ascii=False) == before
    assert [(m.timestamp, m.text) for m in p.messages] == [(t["timestamp"], t["text"]) for t in rec["transcript"]]
