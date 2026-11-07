import json
from decimal import Decimal

import pytest

from backend.scoring import AnswerError, score, to_jsonable
from tests.conftest import ROOT

# Записи чернового эталона, где total не равен расчёту по формуле SPEC
# (арифметика в разметке, файлы eval/*.json менять нельзя). strict=True:
# как только человек исправит total, тест начнёт проходить и потребует убрать запись отсюда.
KNOWN_GOLD_TOTAL_ERRORS = {
    "p1-ru-003": "по SPEC (10+15+10+15+10.5+0)/80 = 75.63, в эталоне 76.88",
    "p1-ru-029": "по SPEC (10+20+0+15+0)/65 = 69.23, в эталоне 71.92",
    "p1-en-014": "по SPEC (10+0+10+0+10.5+0)/85 = 35.88, в эталоне 38.53",
}


def _gold_cases():
    for name in ("gold_tune.json", "edge_cases.json"):
        for a in json.loads((ROOT / "eval" / name).read_text(encoding="utf-8"))["annotations"]:
            marks = []
            if a["conversation_id"] in KNOWN_GOLD_TOTAL_ERRORS:
                marks.append(pytest.mark.xfail(strict=True, reason=KNOWN_GOLD_TOTAL_ERRORS[a["conversation_id"]]))
            yield pytest.param(a, id=f"{name}:{a['conversation_id']}", marks=marks)


@pytest.mark.parametrize("ann", list(_gold_cases()))
def test_gold_total_matches(ann, checklist_v1):
    if ann["answers"] is None:          # not_evaluable: баллы не считаются
        assert ann["total"] is None
        return
    result = score(checklist_v1, ann["answers"])
    assert result.total == Decimal(str(ann["total"])), ann["conversation_id"]


def test_spec_example(checklist_v1):
    r = score(checklist_v1, {"c01": "yes", "c02": "na", "c03": "na", "c04": 15,
                             "c05": "yes", "c06": 7, "c07": "yes", "c08": "no"})
    assert r.total == Decimal("85.38")
    pts = {c.criterion_id: c.points for c in r.criteria}
    assert pts["c04"] == Decimal(15) and pts["c06"] == Decimal("10.5")
    assert pts["c02"] is None and pts["c08"] is None


def test_sections_and_flags(checklist_v1):
    r = score(checklist_v1, {"c01": "yes", "c02": "na", "c03": "no", "c04": 10,
                             "c05": "yes", "c06": 10, "c07": "na", "c08": "yes"})
    s = {x.id: x for x in r.sections}
    assert s["start"].score == Decimal("100.00") and s["start"].max_points == 10
    assert s["safety"].score == Decimal("25.00")           # (0 + 10) / 40
    assert s["conduct"].score == Decimal("100.00")         # (15 + 15) / 30, c07 N/A
    assert s["gross"].score is None                        # только flag — нет весов
    assert [(f.criterion_id, f.answer) for f in r.flags] == [("c08", "yes")]
    assert r.total == Decimal("62.50")                     # (10 + 0 + 10 + 15 + 15) / 80
    assert to_jsonable(r)["total"] == 62.5


def test_all_na_gives_null():
    cl = {"sections": [{"id": "s", "title": "S"}],
          "criteria": [{"id": "a", "section": "s", "title": "A", "type": "binary", "weight": 10,
                        "na_allowed": True, "na_condition": "x"},
                       {"id": "f", "section": "s", "title": "F", "type": "flag", "weight": 0, "na_allowed": False}]}
    r = score(cl, {"a": "na", "f": "no"})
    assert r.total is None and r.sections[0].score is None


def test_round_half_up():
    # 1/8*100 = 12.5 → 13 при 0 знаков (ROUND_HALF_UP, не банковское)
    cl = {"settings": {"rounding_decimals": 0}, "sections": [{"id": "s", "title": "S"}],
          "criteria": [{"id": "a", "section": "s", "title": "A", "type": "scale", "weight": 8,
                        "na_allowed": False, "scale": [{"value": 0}, {"value": 1}, {"value": 8}]}]}
    assert score(cl, {"a": 1}).total == Decimal("13")
    # 1/800*100 = 0.125 → 0.13 (банковское округление дало бы 0.12)
    cl2 = {"sections": [{"id": "s", "title": "S"}],
           "criteria": [{"id": "a", "section": "s", "title": "A", "type": "scale", "weight": 800,
                         "na_allowed": False, "scale": [{"value": 0}, {"value": 1}, {"value": 800}]}]}
    assert score(cl2, {"a": 1}).total == Decimal("0.13")


@pytest.mark.parametrize("answers,code", [
    ({"c01": "na"}, "na_not_allowed"),
    ({"c04": 12}, "invalid_scale_value"),
    ({"c04": "yes"}, "invalid_scale_value"),
    ({"c01": 1}, "invalid_binary_value"),
    ({"c99": "yes"}, "unknown_criterion"),
])
def test_invalid_answers(answers, code, checklist_v1):
    base = {"c01": "yes", "c02": "na", "c03": "na", "c04": 20, "c05": "yes", "c06": 10, "c07": "yes", "c08": "no"}
    with pytest.raises(AnswerError) as e:
        score(checklist_v1, {**base, **answers})
    assert e.value.code == code


def test_missing_answer(checklist_v1):
    with pytest.raises(AnswerError) as e:
        score(checklist_v1, {"c01": "yes"})
    assert e.value.code == "missing_criterion"


def test_new_criterion_without_code_change(checklist_v1):
    cl = json.loads(json.dumps(checklist_v1))
    cl["criteria"].append({"id": "c09", "section": "conduct", "title": "Новый", "type": "scale", "weight": 6,
                           "na_allowed": False, "scale": [{"value": 0}, {"value": 1}, {"value": 2}, {"value": 3}]})
    r = score(cl, {"c01": "yes", "c02": "na", "c03": "na", "c04": 20, "c05": "yes", "c06": 10,
                   "c07": "yes", "c08": "no", "c09": 2})
    assert r.total == Decimal("97.18")      # (65 + 4) / 71
