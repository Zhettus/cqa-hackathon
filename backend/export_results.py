"""Формат сдачи организаторам: экспорт results/<набор>/results.json и проверка такого файла.

- Сопоставление наших критериев с кодами и допустимыми значениями организаторов — `config/export_mapping.json`
  (чек-лист не меняется; новый критерий требует только строки в этом файле).
- Экспортируется АВТОМАТИЧЕСКАЯ оценка системы (исходная, без решений по апелляциям) по заданной
  (замороженной) версии чек-листа: последняя оценка каждого диалога этой версией.
- `evidence_turns` — номера реплик с 1 по ИСХОДНОМУ массиву `transcript` из API. Предобработка не удаляет
  реплики (дубли только помечаются флагом), поэтому наш `idx`/`n` и есть исходная позиция; это закреплено
  тестом `tests/test_export_results.py` (p4-ru-001 с дублем и чат).
- Наши статусы: completed → ok; failed / provider_error → error с причиной; not_evaluable → error
  «В диалоге нет реплик оператора»; нет оценки → error «Оценка не выполнена».
- `model.name` — все фактически использованные модели по llm_calls экспортируемых оценок (включая
  нормализацию звонков, если она входила в оценку); `mode` — local / external_api / hybrid.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

from backend import db
from backend.config import ROOT
from backend.preprocess import preprocess
from backend.scoring import AnswerError, score

MAPPING_PATH = ROOT / "config" / "export_mapping.json"
MODES = ("local", "external_api", "hybrid")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal")
LOCAL_PROVIDERS = ("ollama", "vllm", "lmstudio")
ROOT_KEYS = {"schema_version", "team_name", "model", "results"}
OK_KEYS = {"conversation_id", "status", "criteria", "score"}
ERROR_KEYS = OK_KEYS | {"error"}
CRITERION_KEYS = {"criterion_id", "value", "rationale", "evidence_turns"}
SCORE_KEYS = {"final_score", "critical_flag"}
_PLACEHOLDER = re.compile(r"^\s*$|\[.*\]")         # пусто или «[НАЗВАНИЕ КОМАНДЫ]» — имя команды не заполнено
_SENTENCE = re.compile(r"(?<=[.!?…])\s+")


# --- сопоставление значений --------------------------------------------------------------------
def load_mapping(path: Path | None = None) -> dict:
    return json.loads((path or MAPPING_PATH).read_text(encoding="utf-8"))


def to_org_value(answer: Any, mapping: dict) -> Any:
    """yes/no/na → true/false/null; числа шкалы — как есть."""
    if isinstance(answer, str):
        if answer not in mapping["values"]:
            raise ValueError(f"неизвестный ответ {answer!r}")
        return mapping["values"][answer]
    return answer


def from_org_value(value: Any, mapping: dict) -> Any:
    """true/false/null → yes/no/na; числа — как есть (обратное к to_org_value)."""
    if isinstance(value, bool) or value is None:
        for k, v in mapping["values"].items():
            if v is value:
                return k
        raise ValueError(f"нет нашего ответа для значения {value!r}")
    return value


def _same(value: Any, allowed: Any) -> bool:
    """Строгое сравнение: true ≠ 1, null ≠ 0."""
    if value is None or allowed is None:
        return value is allowed
    if isinstance(value, bool) or isinstance(allowed, bool):
        return isinstance(value, bool) and isinstance(allowed, bool) and value == allowed
    return isinstance(value, (int, float)) and value == allowed


def short_rationale(text: str, max_sentences: int = 2, max_chars: int = 400) -> str:
    """1–2 предложения нашего обоснования; примечание guard («[Правило …]») сохраняется."""
    parts = [p for p in _SENTENCE.split(" ".join((text or "").split())) if p]
    keep = parts[:max_sentences] + [p for p in parts[max_sentences:] if "[Правило" in p]
    out = " ".join(keep).strip()
    if len(out) > max_chars:
        out = out[:max_chars - 1].rstrip() + "…"
    return out or "Обоснование отсутствует."


def evidence_turns(result: dict, fallback: int, n_turns: int) -> list[int]:
    """Номера реплик-доказательств: цитаты → иначе проверенный диапазон (пропущенное действие, N/A) →
    иначе запасная реплика. Всегда непустой список в пределах транскрипта."""
    ev = result.get("evidence") or {}
    turns = [int(q["n"]) for q in ev.get("quotes") or [] if q.get("n") is not None]
    rng = ev.get("range") or {}
    if not turns and rng.get("from_n") is not None and rng.get("to_n") is not None:
        a, b = int(rng["from_n"]), int(rng["to_n"])
        turns = list(range(min(a, b), max(a, b) + 1))
    turns = sorted({t for t in turns if 1 <= t <= n_turns})
    return turns or [max(1, min(fallback, n_turns))]


def _fallback_turn(record: dict, settings: dict) -> int:
    """Запасная реплика (старые оценки без диапазона у N/A): первая реплика клиента до подключения оператора."""
    pre = preprocess(record, settings)
    first_op = pre.facts.first_operator_idx or (len(pre.messages) + 1)
    return next((m.idx for m in pre.messages if m.speaker == "client" and m.idx < first_op), 1)


# --- экспорт ------------------------------------------------------------------------------------
def _error(conversation_id: str, reason: str) -> dict:
    return {"conversation_id": conversation_id, "status": "error", "criteria": [], "score": None,
            "error": (reason or "ошибка оценки").strip()}


def _kind(provider: str | None, base_url: str | None) -> str:
    if (provider or "").lower() in LOCAL_PROVIDERS or (base_url and any(h in base_url for h in LOCAL_HOSTS)):
        return "local"
    return "external_api"


def models_used(conn: sqlite3.Connection, evaluation_ids: list[str]) -> dict:
    """model для сдачи: все модели из llm_calls экспортируемых оценок (нормализация звонков логируется с тем же
    evaluation_id); mode — local, если все вызовы к локальному серверу, external_api — если все к внешнему, иначе hybrid."""
    names: list[str] = []
    kinds: set[str] = set()
    for i in range(0, len(evaluation_ids), 500):
        chunk = evaluation_ids[i:i + 500]
        q = ",".join("?" * len(chunk))
        for provider, model, base_url in conn.execute(
                f"SELECT DISTINCT provider, model, json_extract(request_json, '$.base_url') FROM llm_calls "
                f"WHERE evaluation_id IN ({q})", chunk):
            kinds.add(_kind(provider, base_url))
            if model and model not in names:
                names.append(model)
    mode = "hybrid" if len(kinds) > 1 else ("local" if kinds == {"local"} else "external_api")
    return {"mode": mode, "name": "; ".join(sorted(names)) or "none"}


def export_group(conn: sqlite3.Connection, group_id: str, *, checklist_id: str = "chat-quality",
                 checklist_version: int | None = None, team_name: str | None = None,
                 mapping: dict | None = None) -> dict:
    """Документ формата организаторов по всем диалогам набора group_id."""
    mapping = mapping or load_mapping()
    cl = db.get_checklist(conn, checklist_id, checklist_version)
    convs = db.list_conversations(conn, group_id)
    if not convs:
        raise db.NotFound(f"набор {group_id} не загружен")
    codes = {m["criterion_id"]: m for m in mapping["criteria"]}
    unknown = [c["id"] for c in cl["criteria"] if c["id"] not in codes]
    if unknown:
        raise ValueError(f"в {MAPPING_PATH.name} нет кода организаторов для критериев {unknown}")
    latest = db.latest_evaluations(conn, checklist_id=cl["id"], checklist_version=cl["version"])
    settings = cl.get("settings") or {}
    results, ev_ids = [], []
    for conv in convs:
        cid = conv["conversation_id"]
        summary = latest.get(cid)
        if summary is None:
            results.append(_error(cid, mapping["errors"]["missing"]))
            continue
        ev = db.get_evaluation(conn, summary["id"])
        ev_ids.append(ev["id"])
        if ev["status"] == "not_evaluable":
            results.append(_error(cid, mapping["errors"]["not_evaluable"]))
            continue
        if ev["status"] != "completed":
            results.append(_error(cid, ev.get("status_reason") or ev["status"]))
            continue
        if ev.get("total") is None:
            results.append(_error(cid, mapping["errors"]["no_total"]))
            continue
        by_id = {r["criterion_id"]: r for r in ev["criteria"]}
        n_turns = len(conv["transcript"])
        fallback: int | None = None
        criteria = []
        for crit in cl["criteria"]:
            r = by_id.get(crit["id"])
            if r is None:
                criteria = None
                results.append(_error(cid, f"в оценке нет результата по критерию {crit['id']}"))
                break
            if fallback is None and not (r.get("evidence") or {}).get("quotes") and not (r.get("evidence") or {}).get("range"):
                fallback = _fallback_turn(conv, settings)
            criteria.append({"criterion_id": codes[crit["id"]]["code"], "value": to_org_value(r["answer"], mapping),
                             "rationale": short_rationale(r.get("explanation") or ""),
                             "evidence_turns": evidence_turns(r, fallback or 1, n_turns)})
        if criteria is None:
            continue
        critical = next((c["value"] for c in criteria if c["criterion_id"] == mapping["critical_flag_code"]), False)
        results.append({"conversation_id": cid, "status": "ok", "criteria": criteria,
                        "score": {"final_score": round(float(ev["total"]), 2), "critical_flag": bool(critical)}})
    return {"schema_version": mapping["schema_version"],
            "team_name": team_name or os.getenv("TEAM_NAME") or mapping["team_name"],
            "model": models_used(conn, ev_ids), "results": results}


def write_document(doc: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "results.json"
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


# --- проверка файла -------------------------------------------------------------------------------
def transcript_lengths(records: list[dict]) -> dict[str, int]:
    return {r["conversation_id"]: len(r["transcript"]) for r in records}


def validate_document(doc: Any, *, lengths: dict[str, int], checklist: dict, mapping: dict | None = None,
                      expected_count: int | None = None) -> list[str]:
    """Все правила формата. Пустой список — файл можно сдавать."""
    mapping = mapping or load_mapping()
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["корень файла — не JSON-объект"]
    if set(doc) != ROOT_KEYS:
        errors.append(f"ключи корня {sorted(doc)}, ожидаются {sorted(ROOT_KEYS)}")
    if doc.get("schema_version") != mapping["schema_version"]:
        errors.append(f"schema_version = {doc.get('schema_version')!r}, ожидается {mapping['schema_version']!r}")
    team = doc.get("team_name")
    if not isinstance(team, str) or _PLACEHOLDER.search(team):
        errors.append("team_name не заполнено (пусто или плейсхолдер в квадратных скобках)")
    model = doc.get("model")
    if not isinstance(model, dict) or set(model) != {"mode", "name"}:
        errors.append("model должен быть объектом {mode, name}")
    else:
        if model.get("mode") not in MODES:
            errors.append(f"model.mode = {model.get('mode')!r}, допустимо {', '.join(MODES)}")
        if not isinstance(model.get("name"), str) or not model["name"].strip():
            errors.append("model.name пуст")
    results = doc.get("results")
    if not isinstance(results, list):
        errors.append("results должен быть списком")
        return errors
    expected = len(lengths) if expected_count is None else expected_count
    if len(results) != expected:
        errors.append(f"записей {len(results)}, ожидается {expected}")
    ids = [r.get("conversation_id") for r in results if isinstance(r, dict)]
    dups = sorted({i for i in ids if ids.count(i) > 1}, key=str)
    if dups:
        errors.append(f"повторяющиеся conversation_id: {dups}")
    missing, extra = sorted(set(lengths) - set(ids)), sorted(set(ids) - set(lengths), key=str)
    if missing:
        errors.append(f"нет записей для диалогов набора: {missing}")
    if extra:
        errors.append(f"записи с conversation_id, которых нет в наборе: {extra}")
    for i, rec in enumerate(results):
        errors += _validate_record(rec, i, lengths, checklist, mapping)
    return errors


def _validate_record(rec: Any, i: int, lengths: dict[str, int], checklist: dict, mapping: dict) -> list[str]:
    cid = rec.get("conversation_id") if isinstance(rec, dict) else None
    tag = f"[{i}] {cid}"
    if not isinstance(rec, dict):
        return [f"[{i}]: запись — не объект"]
    e: list[str] = []
    if not isinstance(cid, str) or not cid:
        e.append(f"{tag}: conversation_id должен быть непустой строкой")
    status = rec.get("status")
    if status == "error":
        if set(rec) != ERROR_KEYS:
            e.append(f"{tag}: ключи error-записи {sorted(rec)}, ожидаются {sorted(ERROR_KEYS)}")
        if rec.get("criteria") != []:
            e.append(f"{tag}: у error-записи criteria должен быть []")
        if rec.get("score") is not None:
            e.append(f"{tag}: у error-записи score должен быть null")
        if not isinstance(rec.get("error"), str) or not rec["error"].strip():
            e.append(f"{tag}: error должен быть непустой строкой с причиной")
        return e
    if status != "ok":
        return e + [f"{tag}: status = {status!r}, допустимо ok | error"]
    if set(rec) != OK_KEYS:
        e.append(f"{tag}: ключи записи {sorted(rec)}, ожидаются {sorted(OK_KEYS)}")
    codes = [m["code"] for m in mapping["criteria"]]
    allowed = {m["code"]: m["allowed"] for m in mapping["criteria"]}
    code2cid = {m["code"]: m["criterion_id"] for m in mapping["criteria"]}
    crits = rec.get("criteria")
    if not isinstance(crits, list):
        return e + [f"{tag}: criteria должен быть списком"]
    seen = [c.get("criterion_id") for c in crits if isinstance(c, dict)]
    for code in codes:
        if seen.count(code) != 1:
            e.append(f"{tag}: критерий {code} встречается {seen.count(code)} раз, должен ровно 1")
    for code in seen:
        if code not in allowed:
            e.append(f"{tag}: неизвестный критерий {code!r}")
    n_turns = lengths.get(cid)
    answers: dict[str, Any] = {}
    for c in crits:
        if not isinstance(c, dict):
            e.append(f"{tag}: элемент criteria — не объект")
            continue
        code = c.get("criterion_id")
        if set(c) != CRITERION_KEYS:
            e.append(f"{tag}/{code}: ключи {sorted(c)}, ожидаются {sorted(CRITERION_KEYS)}")
        if code in allowed:
            if not any(_same(c.get("value"), a) for a in allowed[code]):
                e.append(f"{tag}/{code}: value = {c.get('value')!r}, допустимо {allowed[code]}")
            else:
                answers[code2cid[code]] = from_org_value(c.get("value"), mapping)
        if not isinstance(c.get("rationale"), str) or not c["rationale"].strip():
            e.append(f"{tag}/{code}: rationale пуст")
        turns = c.get("evidence_turns")
        if not isinstance(turns, list) or not turns:
            e.append(f"{tag}/{code}: evidence_turns должен быть непустым списком номеров реплик")
        else:
            bad = [t for t in turns if isinstance(t, bool) or not isinstance(t, int) or t < 1
                   or (n_turns is not None and t > n_turns)]
            if bad:
                e.append(f"{tag}/{code}: evidence_turns вне 1..{n_turns}: {bad}")
    sc = rec.get("score")
    if not isinstance(sc, dict) or set(sc) != SCORE_KEYS:
        return e + [f"{tag}: score должен быть объектом {{final_score, critical_flag}}"]
    fs = sc.get("final_score")
    if isinstance(fs, bool) or not isinstance(fs, (int, float)):
        e.append(f"{tag}: final_score должен быть числом")
    else:
        if round(fs, 2) != fs or not 0 <= fs <= 100:
            e.append(f"{tag}: final_score = {fs} — нужно число 0–100 с не более чем 2 знаками")
        if len(answers) == len(codes) and not any(x.startswith(f"{tag}/") for x in e):
            try:
                total = score(checklist, answers).total
            except AnswerError as ex:
                e.append(f"{tag}: значения не принимаются нашим scoring: {ex}")
            else:
                if total is None:
                    e.append(f"{tag}: по значениям итог не вычисляется (нет применимых критериев), а final_score задан")
                elif Decimal(str(fs)).quantize(Decimal("0.01")) != total:
                    e.append(f"{tag}: final_score {fs} ≠ пересчёт из value {total}")
    cf = sc.get("critical_flag")
    if not isinstance(cf, bool):
        e.append(f"{tag}: critical_flag должен быть bool")
    else:
        crit_val = next((c.get("value") for c in crits if isinstance(c, dict)
                         and c.get("criterion_id") == mapping["critical_flag_code"]), None)
        if isinstance(crit_val, bool) and crit_val != cf:
            e.append(f"{tag}: critical_flag = {cf}, а value критерия {mapping['critical_flag_code']} = {crit_val}")
    return e


def validate_file(path: Path, *, lengths: dict[str, int], checklist: dict, mapping: dict | None = None,
                  expected_count: int | None = None) -> list[str]:
    """Файл целиком: валидный UTF-8, валидный JSON, затем все правила документа."""
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as ex:
        return [f"файл не в UTF-8: {ex}"]
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as ex:
        return [f"файл не является валидным JSON: {ex}"]
    return validate_document(doc, lengths=lengths, checklist=checklist, mapping=mapping, expected_count=expected_count)
