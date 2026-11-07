"""Нормализация текста звонков моделью (этап 5).

Транскрипт звонка — шумное автораспознавание. Перед оценкой модель-корректор возвращает для каждой
реплики клиента/оператора text_clean (исправленный текст, смысл не меняется) и asr_uncertain (фрагмент
мог быть распознан неверно). Оригинальные text/timestamp не трогаются (инвариант 8): text_clean идёт
в отдельное поле clean_text, флаг — в flags. Результат кэшируется в таблице normalizations по
(диалог, хеш транскрипта, версия корректора, модель); каждый вызов модели логируется в llm_calls.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from backend import db
from backend.llm import LLMError, LLMProvider
from backend.preprocess import ASR_UNCERTAIN, DUPLICATE, Preprocessed, clean

NORMALIZER_SCHEME = "n1"
MAX_REPAIRS = 2
_MASKED = re.compile(r"\[[^\]]*\]")

SYSTEM = """Ты — корректор автоматического распознавания речи (ASR) для записей звонков контакт-центра
брокерской компании. Тебе дают реплики звонка (клиент и оператор) с номерами. Для КАЖДОЙ реплики верни:
- text_clean — исправленный текст: восстанови пунктуацию и границы слов, исправь явные ошибки
  распознавания (искажённые, слипшиеся или разорванные слова, очевидные артефакты ASR), сохраняя
  смысл, язык реплики и порядок фраз. Ничего не добавляй и не убирай по смыслу, не пересказывай,
  не переводи, не улучшай и не смягчай речь оператора. Маскированные данные вида «[…]»
  (например, «[Имя скрыто]») переноси дословно. Числа, коды, названия бумаг и тикеры не меняй,
  если не уверен. Если реплика в порядке — верни её как есть.
- asr_uncertain — true, если фрагмент настолько искажён, что смысл неясен или ключевое слово
  (сумма, срок, да/нет, название, действие) могло быть распознано неверно; иначе false.
Верни JSON по схеме: items — по одному объекту на каждую переданную реплику, с тем же idx."""

SCHEMA = {"type": "object",
          "properties": {"items": {"type": "array", "items": {
              "type": "object",
              "properties": {"idx": {"type": "integer"}, "text_clean": {"type": "string"},
                             "asr_uncertain": {"type": "boolean"}},
              "required": ["idx", "text_clean", "asr_uncertain"], "additionalProperties": False}}},
          "required": ["items"], "additionalProperties": False}


def normalizer_version() -> str:
    h = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:10]
    return f"{NORMALIZER_SCHEME}-{h}"


def text_hash(pre: Preprocessed) -> str:
    return hashlib.sha256(json.dumps([(m.idx, m.text) for m in pre.messages], ensure_ascii=False)
                          .encode("utf-8")).hexdigest()[:16]


def targets(pre: Preprocessed) -> dict[int, str]:
    """Реплики, которые нормализуются: клиент и оператор, без точных дублей."""
    return {m.idx: m.text for m in pre.messages
            if m.speaker in ("client", "operator") and DUPLICATE not in m.flags and m.text.strip()}


def build_user(pre: Preprocessed, items: dict[int, str]) -> str:
    speaker = {m.idx: m.speaker for m in pre.messages}
    lines = [f"НОРМАЛИЗАЦИЯ ТРАНСКРИПТА ЗВОНКА {pre.conversation_id}",
             f"Язык: {pre.language or 'не указан'}. Реплик: {len(items)}.", "", "Реплики:"]
    lines += [f"#{idx} [{'клиент' if speaker[idx] == 'client' else 'оператор'}]: {text}" for idx, text in items.items()]
    return "\n".join(lines)


def validate(resp: dict, items: dict[int, str]) -> list[str]:
    """Ошибки ответа корректора: набор idx, пустой текст, потерянные маскированные данные."""
    errs: list[str] = []
    out = resp.get("items")
    if not isinstance(out, list):
        return ["items должен быть списком"]
    seen = Counter(o.get("idx") for o in out if isinstance(o, dict))
    missing = sorted(set(items) - set(seen))
    extra = sorted(set(seen) - set(items))
    dups = sorted(i for i, n in seen.items() if n > 1)
    if missing:
        errs.append(f"нет реплик {missing}")
    if extra:
        errs.append(f"лишние idx {extra}")
    if dups:
        errs.append(f"повторяются idx {dups}")
    for o in out:
        if not isinstance(o, dict) or o.get("idx") not in items:
            continue
        idx, tc = o["idx"], o.get("text_clean")
        if not isinstance(tc, str) or not tc.strip():
            errs.append(f"#{idx}: пустой text_clean")
            continue
        if Counter(_MASKED.findall(items[idx])) != Counter(_MASKED.findall(tc)):
            errs.append(f"#{idx}: маскированные данные «[…]» должны быть перенесены дословно")
        if not isinstance(o.get("asr_uncertain"), bool):
            errs.append(f"#{idx}: asr_uncertain — true/false")
    return errs


async def normalize_call(conn, provider: LLMProvider, pre: Preprocessed, *, evaluation_id: str | None = None,
                         batch_id: str | None = None, use_cache: bool = True) -> tuple[dict[int, dict], bool]:
    """Возвращает ({idx: {text_clean, asr_uncertain}}, из_кэша). Вызовы модели логируются в llm_calls."""
    items = targets(pre)
    version, th = normalizer_version(), text_hash(pre)
    if use_cache:
        cached = db.get_normalization(conn, pre.conversation_id, th, version, provider.model)
        if cached is not None:
            return cached, True
    if not items:
        db.insert_normalization(conn, pre.conversation_id, th, version, provider.model, {})
        return {}, False
    user = build_user(pre, items)
    feedback = None
    last = "нет ответа"
    for attempt in range(1, MAX_REPAIRS + 2):
        call = {"evaluation_id": evaluation_id, "conversation_id": pre.conversation_id, "batch_id": batch_id,
                "attempt": attempt, "provider": provider.name, "model": provider.model, "started_at": db.now(),
                "request": {**provider.describe_request(SYSTEM, user + (f"\n\n{feedback}" if feedback else ""),
                                                        SCHEMA, None), "purpose": "normalize",
                            "normalizer_version": version}}
        try:
            resp, usage = await provider.complete_json(SYSTEM, user + (f"\n\n{feedback}" if feedback else ""), SCHEMA)
        except LLMError as e:
            _log(conn, call, e.usage, status="invalid_response" if e.code == "bad_json" else "provider_error",
                 errors=[{"code": e.code, "message": str(e)}])
            if e.code != "bad_json" or e.usage is None:
                raise
            last = str(e)
            feedback = "ПРЕДЫДУЩИЙ ОТВЕТ ОТКЛОНЁН: это не корректный JSON по схеме. Верни корректный JSON."
            continue
        errs = validate(resp, items)
        _log(conn, call, usage, status="invalid_response" if errs else "ok",
             errors=[{"code": "normalize", "message": m} for m in errs])
        if not errs:
            out = {int(o["idx"]): {"text_clean": clean(o["text_clean"]), "asr_uncertain": bool(o["asr_uncertain"])}
                   for o in resp["items"]}
            db.insert_normalization(conn, pre.conversation_id, th, version, provider.model, out)
            return out, False
        last = "; ".join(errs)
        feedback = "ПРЕДЫДУЩИЙ ОТВЕТ ОТКЛОНЁН ВАЛИДАТОРОМ:\n- " + "\n- ".join(errs) + "\nИсправь и верни JSON заново."
    raise LLMError(f"нормализация {pre.conversation_id}: ответ не прошёл проверку после {MAX_REPAIRS} повторов: {last}",
                   code="invalid_response")


def _log(conn, call: dict, usage, *, status: str, errors: list[dict]) -> None:
    if usage is not None:
        call.update(model=usage.model, latency_ms=usage.latency_ms, cost_usd=usage.cost_usd,
                    tokens_in=usage.tokens_in + usage.cache_read_tokens + usage.cache_write_tokens,
                    tokens_out=usage.tokens_out, raw_response=usage.raw_response)
    db.insert_llm_call(conn, {**call, "status": status, "validation_errors": errors})


def apply_normalization(pre: Preprocessed, norm: dict[int, dict]) -> Preprocessed:
    """Копия предобработки с clean_text = text_clean и флагом asr_uncertain. Оригинальный text не меняется."""
    out = pre.model_copy(deep=True)
    for m in out.messages:
        n = norm.get(m.idx)
        if not n:
            continue
        m.clean_text = n["text_clean"]
        if n.get("asr_uncertain") and ASR_UNCERTAIN not in m.flags:
            m.flags.append(ASR_UNCERTAIN)
    return out
