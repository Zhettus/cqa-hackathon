"""Сборка промпта из конфига критерия и фактов предобработки.

Структура вызова (один вызов = один критерий):
  system        — роль, общие правила, полный docs/rules.md, правила для звонков (стабильно → кэш);
  cache_prefix  — канал, язык, факты, транскрипт (общий для всех критериев диалога → кэш);
  user          — описание критерия, подсказки, формат ответа.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from backend.preprocess import Preprocessed

RULES_PATH = Path(__file__).resolve().parent.parent / "docs" / "rules.md"
PROMPT_SCHEME = "v1"   # меняйте вручную при смысловой правке; хеш ниже ловит любую правку текста


def prompt_version(normalized: bool = False) -> str:
    """Версия промпта для кэша оценок: схема + хеш этого файла и docs/rules.md;
    для звонков с нормализацией — плюс версия корректора."""
    h = hashlib.sha256(Path(__file__).read_bytes() + RULES_PATH.read_bytes()).hexdigest()[:10]
    base = f"{PROMPT_SCHEME}-{h}"
    if normalized:
        from backend.normalize import normalizer_version
        return f"{base}+{normalizer_version()}"
    return base


FLAG_VIOLATION_TYPES = ["none", "insult", "off_platform_paid_service", "confidential_disclosure", "other"]

SPEAKER_RU = {"client": "клиент", "operator": "оператор", "bot": "бот", "system": "система"}
FLAG_RU = {"ivr": "IVR", "client_not_hearing": "клиент_не_слышит", "after_end": "после_завершения",
           "dtmf": "DTMF", "duplicate": "дубль", "asr_uncertain": "ASR_неуверенно"}

BASE_SYSTEM = """Ты — эксперт отдела контроля качества контакт-центра брокерской компании Freedom Broker.
Ты оцениваешь работу ОПЕРАТОРА в одном диалоге по ОДНОМУ критерию чек-листа.

Общие правила:
1. Оценивается только оператор. Реплики бота (speaker=бот), системные события, IVR, реплики,
   помеченные флагами IVR / клиент_не_слышит / после_завершения, оператору не засчитываются —
   ни в плюс, ни в минус. Реплики с флагом клиент_не_слышит — это разговор оператора с коллегой
   или реплики на удержании: клиент их не слышал.
2. Следуй формулировке критерия буквально. Не добавляй требований, которых в ней нет,
   и не смягчай те, что есть. Трактовки правил ниже обязательны и имеют приоритет над общими соображениями.
3. N/A допустим только если для критерия разрешён N/A и выполнено его условие неприменимости.
   Если N/A не разрешён — выбери один из допустимых ответов.
4. Для шкалы выбирай только из перечисленных ступеней.
5. Факты о времени (первая реакция, паузы, удержание), идентификации и завершении посчитаны
   программно и точны — используй их, не пересчитывай время сам.
6. Сообщения клиента до подключения оператора (в том числе боту) — это детали, уже известные оператору.
7. Доказательства:
   - действие выполнено (или нарушение найдено) → evidence: номер реплики (idx) и КОРОТКАЯ дословная
     цитата из её текста (копируй символы как в транскрипте, на языке оригинала, без перевода и пересказа);
   - действие пропущено → evidence может быть пустым, укажи checked_range — диапазон реплик, где
     действие ожидалось и было проверено;
   - N/A → na_reason: какое условие неприменимости выполнено.
8. reasoning — 1–3 предложения на русском: почему выбран именно этот ответ со ссылками на #idx.
9. Поля ответа заполняй в порядке схемы: сначала доказательства, потом рассуждение, в конце ответ.
10. Недостаток оператора оценивается в том критерии, чья формулировка его описывает. Не снижай
   текущий критерий за недостаток, который относится к другому критерию чек-листа (их список дан
   вместе с критерием). Как делятся смежные случаи и когда флаг грубого нарушения ставится вместе
   со снижением другого критерия — в трактовках ниже.

Трактовки правил чек-листа (docs/rules.md, обязательны):
"""

CALL_RULES = """
Правила для звонков:
- Текст — автоматическое распознавание речи: в нём есть ошибки, искажённые слова и имена,
  обрывки фраз, повторы. Оценивай СМЫСЛ сказанного. Ошибки распознавания не считаются ошибками
  оператора (в т.ч. грамматика, «странные» слова, неверно распознанное имя).
- Персональные данные замаскированы («[Имя скрыто]», «[Персональные данные скрыты]» и т.п.):
  «меня зовут [Имя скрыто]» значит, что оператор представился; маскировка — не ошибка.
- Цифры «1», «2», «#» от клиента в начале звонка — тоновый набор в IVR (флаг DTMF).
- Идентификация в звонке словесная: ФИО, ИИН, адрес, SMS-код. Подтверждение — системным событием
  или словами оператора после того, как клиент назвал данные.
"""

NORMALIZED_RULES = """- Строка «clean:» под репликой — текст после корректора распознавания; читай по ней, сверяясь
  с оригиналом. Флаг ASR_неуверенно — фрагмент мог быть распознан неверно: не ставь оператору в вину
  формулировки из таких фрагментов и не делай по ним выводов о раскрытии данных или ошибочном совете.
"""


def build_system(channel: str, normalized: bool = False) -> str:
    rules = RULES_PATH.read_text(encoding="utf-8").strip()
    call = (CALL_RULES + (NORMALIZED_RULES if normalized else "")) if channel == "voice call" else ""
    return BASE_SYSTEM + rules + "\n" + call


def _mmss(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 60:02d}:{s % 60:02d}"


def _time(pre: Preprocessed, m) -> str:
    return m.timestamp if pre.channel != "voice call" else f"+{_mmss(m.t_s)}"


def format_transcript(pre: Preprocessed) -> str:
    lines = []
    for m in pre.visible_messages():
        flags = ", ".join(FLAG_RU.get(f, f) for f in m.flags)
        head = f"[#{m.idx} | {SPEAKER_RU.get(m.speaker, m.speaker)} | {_time(pre, m)} | {flags}]"
        lines.append(f"{head} {m.text}")
        if m.clean_text and m.clean_text != m.text:
            lines.append(f"    clean: {m.clean_text}")
    return "\n".join(lines)


def format_facts(pre: Preprocessed, settings: dict) -> str:
    f = pre.facts
    thr = settings.get("first_response_threshold_s", 180)
    pause_thr = f.pause_threshold_s
    lines = [
        f"- Язык клиента: {f.client_language or 'не определён'}; язык первой реплики оператора: "
        f"{f.operator_first_reply_language or 'нет реплик'}.",
        f"- Подключение оператора: " + (f"#{f.escalation_idx} ({f.escalation_at})" if f.escalation_idx else "события нет") +
        (f"; первая реплика оператора #{f.first_operator_idx}." if f.first_operator_idx else "; оператор не ответил."),
    ]
    if f.first_response_s is not None:
        verdict = "ПРЕВЫШЕН" if f.first_response_s > thr else "в норме"
        lines.append(f"- Первая реакция оператора: {int(f.first_response_s)} с (порог {int(thr)} с) — {verdict}.")
    long_gaps = [g for g in f.gaps if g.kind == "reply" and g.effective_s >= 60 or g.significant]
    if long_gaps:
        lines.append(f"- Паузы «клиент → оператор» от 60 с (существенная — дольше {int(pause_thr)} с, без учёта удержания):")
        for g in long_gaps:
            mark = "предупреждена" if g.warned else "БЕЗ предупреждения"
            hold = f", из них на удержании {int(g.hold_overlap_s)} с" if g.hold_overlap_s else ""
            sig = "существенная" if g.significant else "несущественная"
            lines.append(f"    #{g.from_idx} → #{g.to_idx}: {int(g.gap_s)} с{hold}; {sig}; {mark}.")
    else:
        lines.append("- Пауз «клиент → оператор» от 60 с нет.")
    lines.append(f"- Максимальная существенная пауза без предупреждения: {int(f.max_unwarned_gap_s)} с.")
    if f.hold_intervals:
        hs = ", ".join(f"#{i.from_idx}–#{i.to_idx or 'конец'} ({int(i.duration_s)} с)" for i in f.hold_intervals)
        lines.append(f"- Удержание: {hs}; всего {int(f.hold_s)} с.")
    if f.consultation_intervals:
        cs = ", ".join(f"#{i.from_idx}–#{i.to_idx or 'конец'}" for i in f.consultation_intervals)
        lines.append(f"- Консультация оператора с коллегой (клиент не слышит): {cs}.")
    if f.identity_verified_idx:
        src = "системное событие" if f.identity_source == "system" else "словесное подтверждение оператора"
        lines.append(f"- Идентификация подтверждена: #{f.identity_verified_idx} ({f.identity_verified_at}), {src}.")
    else:
        lines.append("- Подтверждения идентификации в диалоге нет.")
    closing = {"operator_closed": "оператор закрыл чат", "connection_lost": "связь с клиентом прервалась",
               "client_left": "клиент покинул чат", "client_ended_call": "клиент завершил звонок",
               "none": "системного события завершения нет"}[f.closing_event]
    lines.append(f"- Завершение: {closing}.")
    lines.append(f"- Засчитываемых реплик оператора: {f.operator_messages}.")
    pre_op = [m for m in pre.visible_messages() if m.speaker == "client" and m.segment in ("pre_operator", "ivr")
              and "dtmf" not in m.flags]
    if pre_op:
        lines.append("- Сообщения клиента до подключения оператора (оператор их видит/знает):")
        lines += [f"    #{m.idx}: {m.text}" for m in pre_op]
    return "\n".join(lines)


def build_dialog_block(pre: Preprocessed, settings: dict) -> str:
    return (f"ДИАЛОГ {pre.conversation_id}\nКанал: {pre.channel}. Язык: {pre.language or 'не указан'}.\n\n"
            f"ФАКТЫ (посчитаны программно):\n{format_facts(pre, settings)}\n\n"
            f"ТРАНСКРИПТ (формат: [#номер | говорящий | время | флаги] текст):\n{format_transcript(pre)}")


def _answer_options(criterion: dict) -> str:
    if criterion["type"] == "scale":
        opts = "\n".join(f"    {s['value']} — {s.get('description', '')}"
                         for s in sorted(criterion["scale"], key=lambda s: -s["value"]))
        return f"Тип: шкала. Допустимые ступени answer:\n{opts}"
    if criterion["type"] == "flag":
        return ('Тип: флаг грубого нарушения (баллов не даёт). answer = "yes", если нарушение есть, иначе "no". '
                "violation_type — вид нарушения (none, если его нет).")
    return 'Тип: да/нет. answer = "yes" или "no".'


def build_criterion_block(criterion: dict, hints: list[dict] | None = None, feedback: str | None = None,
                          others: list[dict] | None = None) -> str:
    na = (f'N/A разрешён (answer = "na") только при условии: {criterion.get("na_condition", "")}'
          if criterion.get("na_allowed") else "N/A НЕ разрешён.")
    parts = [f"КРИТЕРИЙ {criterion['id']}: {criterion['title']}",
             f"Описание: {criterion.get('description', '')}",
             _answer_options(criterion), na]
    if others:
        parts.append("ДРУГИЕ КРИТЕРИИ ЧЕК-ЛИСТА (оцениваются отдельно; не снижай текущий критерий за то, что относится к ним):")
        parts += [f"    - {o['title']}: {o.get('description', '')}" for o in others]
    if hints:
        parts.append("ПОДСКАЗКИ (автоматический поиск по шаблонам; могут быть ложными, проверь по смыслу):")
        parts += [f"    #{h['idx']}: «{h['match']}» — {h['label']}" for h in hints]
    elif criterion.get("hints"):
        parts.append("ПОДСКАЗКИ: шаблоны нарушений в репликах оператора не найдены (это не исключает нарушение).")
    parts.append("Оцени оператора по этому критерию и верни JSON по схеме.")
    if feedback:
        parts.append(feedback)
    return "\n".join(parts)


def response_schema(criterion: dict) -> dict:
    """JSON-схема ответа из конфига критерия. Порядок полей — порядок генерации."""
    na = ["na"] if criterion.get("na_allowed") else []
    if criterion["type"] == "scale":
        steps = sorted({s["value"] for s in criterion["scale"]})
        answer = {"anyOf": [{"type": "integer", "enum": steps}] + ([{"type": "string", "enum": na}] if na else [])}
    else:
        answer = {"type": "string", "enum": ["yes", "no"] + na}
    props = {
        "evidence": {"type": "array", "items": {
            "type": "object", "properties": {"idx": {"type": "integer"}, "quote": {"type": "string"}},
            "required": ["idx", "quote"], "additionalProperties": False}},
        "checked_range": {"anyOf": [{"type": "null"}, {
            "type": "object", "properties": {"from": {"type": "integer"}, "to": {"type": "integer"}},
            "required": ["from", "to"], "additionalProperties": False}]},
        "na_reason": {"anyOf": [{"type": "null"}, {"type": "string"}]},
        "reasoning": {"type": "string"},
        "answer": answer,
    }
    if criterion["type"] == "flag":
        props["violation_type"] = {"type": "string",
                                   "enum": criterion.get("violation_types") or FLAG_VIOLATION_TYPES}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}
