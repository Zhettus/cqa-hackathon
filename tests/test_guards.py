import pytest

from backend.checklist import load_default_checklist
from backend.guards import GuardConfigError, apply_guards
from backend.preprocess import preprocess

CL = load_default_checklist()
C06 = next(c for c in CL["criteria"] if c["id"] == "c06")
S = CL["settings"]


def facts(rec):
    return preprocess(rec, S).facts.model_dump()


def test_first_response_over_threshold_caps(edge_records):
    ans, hits = apply_guards(C06, 10, facts(edge_records["edge-ru-001"]), S)       # 311 с > 180
    assert ans == 7 and len(hits) == 1 and hits[0].changed
    text = hits[0].explanation()
    assert "c06_timing" in text and "first_response_s = 311 > 180" in text and "10 → 7" in text


def test_unwarned_gap_caps(edge_records):
    ans, hits = apply_guards(C06, 10, facts(edge_records["edge-ru-002"]), S)       # пауза 634 с
    assert ans == 7 and "max_unwarned_gap_s = 634 > 180" in hits[0].explanation()


def test_lower_answer_kept_but_recorded(edge_records):
    ans, hits = apply_guards(C06, 4, facts(edge_records["edge-ru-002"]), S)
    assert ans == 4 and len(hits) == 1 and not hits[0].changed
    assert "уже соответствует" in hits[0].explanation()


def test_warned_pause_and_hold_not_triggered(edge_records):
    for cid in ("edge-ru-003", "edge-kz-003"):                                       # предупреждена / удержание
        ans, hits = apply_guards(C06, 10, facts(edge_records[cid]), S)
        assert ans == 10 and hits == [], cid


def test_threshold_comes_from_settings(edge_records):
    f = facts(edge_records["edge-ru-001"])
    ans, hits = apply_guards(C06, 10, f, {**S, "first_response_threshold_s": 400, "pause_threshold_s": 400})
    assert ans == 10 and hits == []


def test_cap_snaps_to_allowed_step():
    crit = {"id": "x", "type": "scale", "scale": [{"value": 0}, {"value": 3}, {"value": 6}, {"value": 9}],
            "guards": [{"id": "g", "when": {"all": [{"fact": "a", "op": ">=", "value": 1}]}, "max_answer": 7}]}
    assert apply_guards(crit, 9, {"a": 1}, {})[0] == 6
    assert apply_guards(crit, "na", {"a": 1}, {})[0] == "na"


def test_force_and_min_and_all():
    crit = {"id": "f", "type": "flag", "guards": [
        {"id": "g", "when": {"all": [{"fact": "a", "op": "==", "value": 1}, {"fact": "b", "op": "not_null"}]},
         "force_answer": "yes", "note": "n"}]}
    assert apply_guards(crit, "no", {"a": 1, "b": 0}, {})[0] == "yes"
    assert apply_guards(crit, "no", {"a": 1, "b": None}, {})[0] == "no"
    sc = {"id": "s", "type": "scale", "scale": [{"value": 0}, {"value": 5}, {"value": 10}],
          "guards": [{"id": "m", "when": {"any": [{"fact": "a", "op": "is_null"}]}, "min_answer": 4}]}
    assert apply_guards(sc, 0, {"a": None}, {})[0] == 5


def test_null_fact_does_not_fire(edge_records):
    f = facts(edge_records["edge-ru-005"])          # нет оператора: first_response_s = None
    assert apply_guards(C06, 10, f, S) == (10, [])


@pytest.mark.parametrize("guard", [
    {"id": "g", "when": {"any": [{"fact": "nope", "op": ">", "value": 1}]}, "max_answer": 1},
    {"id": "g", "when": {"any": [{"fact": "a", "op": "~", "value": 1}]}, "max_answer": 1},
    {"id": "g", "when": {"any": [{"fact": "a", "op": ">", "value_from": "x.y"}]}, "max_answer": 1},
    {"id": "g", "when": {}, "max_answer": 1},
])
def test_bad_config(guard):
    with pytest.raises(GuardConfigError):
        apply_guards({"id": "c", "type": "scale", "scale": [{"value": 1}], "guards": [guard]}, 1, {"a": 2}, {})


def test_guard_is_config_not_code():
    crit = {"id": "c99", "type": "binary", "guards": [
        {"id": "late", "when": {"any": [{"fact": "first_response_s", "op": ">", "value": 60}]},
         "force_answer": "no", "note": "поздно"}]}
    assert apply_guards(crit, "yes", {"first_response_s": 61}, {})[0] == "no"
