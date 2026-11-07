import pytest

from backend.checklist import load_default_checklist
from backend.preprocess import preprocess
from backend.validator import QUOTE_MIN_RATIO, quote_ratio, validate_response

CL = {c["id"]: c for c in load_default_checklist()["criteria"]}


@pytest.fixture(scope="module")
def pre(edge_records):
    return preprocess(edge_records["edge-ru-001"], {"first_response_threshold_s": 180})


def resp(**kw):
    base = {"evidence": [{"idx": 10, "quote": "Меня зовут Арман"}], "checked_range": None,
            "na_reason": None, "reasoning": "Поздоровался и представился в #10.", "answer": "yes"}
    return {**base, **kw}


def codes(issues):
    return [i.code for i in issues]


def test_valid(pre):
    assert validate_response(CL["c01"], resp(), pre) == []


def test_na_not_allowed(pre):
    assert codes(validate_response(CL["c01"], resp(answer="na", na_reason="x"), pre)) == ["na_not_allowed"]


def test_na_allowed_needs_reason(pre):
    assert validate_response(CL["c02"], resp(answer="na", evidence=[], na_reason="Данных хватало: #1, #5"), pre) == []
    assert codes(validate_response(CL["c02"], resp(answer="na", evidence=[], na_reason=""), pre)) == ["missing_na_reason"]


@pytest.mark.parametrize("bad", [12, 3, 21, "15", True])
def test_scale_only_allowed_steps(pre, bad):
    assert codes(validate_response(CL["c04"], resp(answer=bad), pre)) == ["invalid_scale_value"]


def test_scale_ok(pre):
    assert validate_response(CL["c04"], resp(answer=15), pre) == []
    assert validate_response(CL["c06"], resp(answer=7), pre) == []


def test_binary_value(pre):
    assert codes(validate_response(CL["c05"], resp(answer="maybe"), pre)) == ["invalid_binary_value"]


def test_unknown_idx(pre):
    assert codes(validate_response(CL["c01"], resp(evidence=[{"idx": 99, "quote": "x"}]), pre)) == ["unknown_idx"]
    assert codes(validate_response(CL["c01"], resp(evidence=[{"idx": 0, "quote": "x"}]), pre)) == ["unknown_idx"]


def test_quote_must_be_in_original_text(pre):
    # цитата из другой реплики
    assert codes(validate_response(CL["c01"], resp(evidence=[{"idx": 11, "quote": "Меня зовут Арман"}]), pre)) \
        == ["quote_not_found"]
    # перевод/пересказ — не цитата
    assert codes(validate_response(CL["c01"], resp(evidence=[{"idx": 10, "quote": "My name is Arman"}]), pre)) \
        == ["quote_not_found"]


def test_quote_fuzzy(pre):
    ok = ["меня зовут Арман", "«Меня зовут Арман.»", "Здравствуйте! Меня зовут Арман",
          "Меня зовут Арманн", "Спасибо за ожидание"]
    for q in ok:
        assert validate_response(CL["c01"], resp(evidence=[{"idx": 10, "quote": q}]), pre) == [], q


def test_quote_ratio_threshold():
    text = "Здравствуйте! Меня зовут Арман. Спасибо за ожидание."
    assert quote_ratio("Меня зовут Арман", text) == 1.0
    assert quote_ratio("Меня завут Арман", text) >= QUOTE_MIN_RATIO     # опечатка
    assert quote_ratio("Меня зовут Олжас", text) < QUOTE_MIN_RATIO      # другое имя
    assert quote_ratio("", text) == 0.0


def test_missing_evidence_and_range(pre):
    assert codes(validate_response(CL["c07"], resp(answer="no", evidence=[]), pre)) == ["missing_evidence"]
    assert validate_response(CL["c07"], resp(answer="no", evidence=[], checked_range={"from": 10, "to": 12}), pre) == []
    assert codes(validate_response(CL["c07"], resp(answer="no", evidence=[], checked_range={"from": 12, "to": 10}),
                                   pre)) == ["bad_range"]
    assert codes(validate_response(CL["c07"], resp(answer="no", evidence=[], checked_range={"from": 1, "to": 40}),
                                   pre)) == ["bad_range"]


def test_bad_shape(pre):
    r = resp()
    del r["reasoning"]
    assert codes(validate_response(CL["c01"], r, pre)) == ["bad_shape"]
    assert codes(validate_response(CL["c01"], resp(reasoning="  "), pre)) == ["bad_shape"]
