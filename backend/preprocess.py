"""Предобработка диалога: нумерация, время, флаги реплик, факты (инвариант 3).

Исходные text/timestamp не меняются (инвариант 8): нормализованный текст —
отдельное поле clean_text. Дубли не удаляются из списка, а помечаются флагом
duplicate и исключаются из фактов и из текста для модели (`visible_messages`).
Нумерация idx — 1-based, совпадает с `n` в REST API.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# --- флаги реплик --------------------------------------------------------------
DUPLICATE = "duplicate"                    # точный дубль предыдущей реплики (ts+speaker+text)
IVR = "ivr"                                # звонок: реплики клиента/бота до подключения оператора
CLIENT_NOT_HEARING = "client_not_hearing"  # удержание или консультация с агентом
AFTER_END = "after_end"                    # после «Клиент завершил разговор»
DTMF = "dtmf"                              # тоновый набор клиента в звонке

# R1: засчитываются все реплики speaker=operator, кроме удержания/консультации, реплик после завершения
# разговора и точных дублей. Положение относительно события эскалации на зачёт не влияет —
# эскалация лишь точка отсчёта первой реакции.
NOT_COUNTED_FLAGS = {DUPLICATE, CLIENT_NOT_HEARING, AFTER_END}

Segment = Literal["pre_operator", "ivr", "operator", "hold", "consultation", "after_end"]
Lang = Literal["RU", "KZ", "EN"]

# --- системные события ------------------------------------------------------------
# Порядок важен: hold_end проверяется раньше hold_start (текст конца содержит текст начала).
_EVENT_PATTERNS: list[tuple[str, str]] = [
    ("escalation", r"эскалация на оператора|чат операторға жіберілді|chat transferred to an agent"),
    ("agent_joined", r"агент присоединился к разговору|agent joined the (conversation|call)"),
    ("identity_verified", r"личность клиента подтверждена|клиенттің жеке басы расталды|customer identity verified"),
    ("operator_closed", r"оператор закрыл чат|оператор чатты жапты|agent closed the chat"),
    ("connection_lost", r"соединение с клиентом прервано|клиентпен байланыс үзілді|connection to customer lost"),
    ("client_left", r"клиент покинул чат|(customer|client) left the chat"),
    ("call_ended", r"клиент завершил разговор|(customer|client) ended the (call|conversation)"),
    ("hold_end", r"клиент больше не находится на удержании|(customer|client) is no longer on hold"),
    ("hold_start", r"клиент находится на удержании|(customer|client) (is|was) (placed )?on hold"),
    ("consult_start", r"инициирована консультация|началась консультация|consult(ation)? (with an agent )?(initiated|started)"),
    ("consult_end", r"агент вышел из разговора|agent left the (conversation|call)"),
    ("recording_paused", r"(запись|жазу).*(приостановлен|тоқтатыл)|recording.*paused"),
    ("recording_resumed", r"(запись|жазу).*(возобновлен|жалғастырыл|қайта)|recording.*resumed"),
    ("recording_started", r"запись и транскрибирование запущены|жазу және транскрипция басталды|recording.*started"),
    ("transfer_to_agent", r"переведен от агента на представителя|transferred from (the )?(virtual )?agent"),
    ("language_changed", r"language(s)? (have been|has been|changed)|dialog language changed"),
]
_EVENTS = [(name, re.compile(p, re.I)) for name, p in _EVENT_PATTERNS]

CLOSING_EVENTS = {
    "operator_closed": "operator_closed",
    "connection_lost": "connection_lost",
    "client_left": "client_left",
    "call_ended": "client_ended_call",
}

# --- словари ----------------------------------------------------------------------
KZ_LETTERS = set("әғқңөұүһі")
# Частые казахские слова без специфических букв (для смешанного текста).
KZ_LEXICON = {"бе", "ба", "ма", "ме", "па", "пе", "керек", "мен", "сен", "бар", "деп", "иә",
              "ия", "жарайды", "болды", "кеше", "бүгін", "қалай", "неге", "және", "бірақ", "осы"}
KZ_SHARE_THRESHOLD = 0.2   # доля «казахских» кириллических слов, начиная с которой текст — KZ

_THANKS_FOR_WAITING = re.compile(
    r"(спасибо|благодарю)( вас)? за ожидание|(извините|простите)( меня)?,? за (долгое )?ожидание"
    r"|thank(s| you) for (waiting|your patience|holding)|sorry (for|about) the (long )?wait"
    r"|күткеніңізге (рақмет|рахмет)|күткеніңіз үшін (рақмет|рахмет)",
    re.I,
)
_WAIT_WARNING = re.compile(
    r"ожида(й|йте|ть)\b|подожд|оставайтесь на линии|не кладите трубку|ожидать на линии"
    r"|(одну|пару|несколько|пару-тройку) (минут|секунд)|минут(у|ку|очку)\b|секунд(у|очку)\b"
    r"|займ[её]т( некоторое)? (время|несколько|пару)|(сейчас )?(уточню|проверю|посмотрю)\b"
    r"|күте тұр|күтіңіз|(бірнеше|екі-үш|бір-екі|бір) минут|тексеріп (шығуым|көрейін|жатырмын)|нақтылап|желіде"
    r"|please (wait|hold)|hold on|(one|a) moment|(few|couple of|couple) minutes|bear with me"
    r"|let me check|stay on the line|give me a (minute|moment|second)",
    re.I,
)
# Словесное подтверждение личности в звонке (после того как клиент назвал скрытые данные).
_VERBAL_ID_CONFIRM = re.compile(
    r"вс[её] верно|идентификаци\w* (успешно|пройден)|успешно (прошли|пройден)|личность подтвержден"
    r"|данные совпада|вы идентифицированы|идентифицировал"
    r"|сәйкестендіру сәтті|жеке бас\w* расталды|бәрі дұрыс"
    r"|identity (has been |is )?(verified|confirmed)|verification (was )?successful|you('re| are) verified",
    re.I,
)
_MASKED = re.compile(r"\[[^\]]*скрыт[^\]]*\]|\[[^\]]*(hidden|masked|redacted)[^\]]*\]", re.I)
_ANY_BRACKET = re.compile(r"\[[^\]]*\]")
_DTMF = re.compile(r"^\s*[0-9*#]{1,6}\s*[.]?\s*$")
_WORD = re.compile(r"[^\W\d_]+", re.U)
_MMSS = re.compile(r"^(?:(\d{1,2}):)?(\d{1,3}):(\d{2})$")


# --- модели ---------------------------------------------------------------------------
class Message(BaseModel):
    idx: int
    timestamp: str
    speaker: str
    text: str
    clean_text: str
    t_s: float
    audio_offset_s: float | None = None
    event: str | None = None
    flags: list[str] = Field(default_factory=list)
    segment: Segment
    counts_for_operator: bool


class Gap(BaseModel):
    """Пауза «клиент → оператор» (или «эскалация → первый ответ»)."""
    kind: Literal["first_response", "reply"]
    from_idx: int
    to_idx: int
    gap_s: float
    hold_overlap_s: float
    effective_s: float       # gap_s минус время удержания внутри паузы
    warned: bool
    significant: bool        # effective_s > pause_threshold_s


class Interval(BaseModel):
    from_idx: int
    to_idx: int | None       # None — не закрыт до конца диалога
    duration_s: float


class Facts(BaseModel):
    channel: str
    client_language: Lang | None
    operator_first_reply_language: Lang | None
    escalation_idx: int | None
    escalation_at: str | None
    first_operator_idx: int | None
    first_response_s: float | None
    pause_threshold_s: float
    gaps: list[Gap]
    max_unwarned_gap_s: float
    warned_gap_s: float
    hold_s: float
    hold_intervals: list[Interval]
    consultation_intervals: list[Interval]
    identity_verified_idx: int | None
    identity_verified_at: str | None
    identity_source: Literal["system", "verbal"] | None
    closing_event: Literal["operator_closed", "connection_lost", "client_left", "client_ended_call", "none"]
    operator_messages: int
    duplicates: list[int]


class Preprocessed(BaseModel):
    conversation_id: str
    channel: str
    language: str | None
    messages: list[Message]
    facts: Facts

    def visible_messages(self) -> list[Message]:
        """Реплики для показа модели: без точных дублей."""
        return [m for m in self.messages if DUPLICATE not in m.flags]


# --- утилиты --------------------------------------------------------------------------
def parse_timestamp(ts: str) -> tuple[str, float]:
    """('mmss', секунды) для MM:SS / H:MM:SS, ('iso', epoch) для ISO 8601."""
    s = ts.strip()
    m = _MMSS.match(s)
    if m:
        h, mm, ss = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
        return "mmss", float(h * 3600 + mm * 60 + ss)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"Неизвестный формат времени: {ts!r}") from e
    return "iso", dt.timestamp()


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


def detect_event(text: str) -> str:
    for name, pat in _EVENTS:
        if pat.search(text):
            return name
    return "other"


def detect_language(texts: list[str]) -> Lang | None:
    """RU/KZ/EN по буквам. Латиница > кириллицы → EN. Иначе KZ, если доля
    казахских слов (казахские буквы или частые казахские слова) ≥ порога."""
    cyr = lat = kz = 0
    for t in texts:
        for w in _WORD.findall(_ANY_BRACKET.sub(" ", t).lower()):
            if any("a" <= ch <= "z" for ch in w) and not any("Ѐ" <= ch <= "ӿ" for ch in w):
                lat += 1
            elif any("Ѐ" <= ch <= "ӿ" for ch in w):
                cyr += 1
                if KZ_LETTERS & set(w) or w in KZ_LEXICON:
                    kz += 1
    if cyr == 0 and lat == 0:
        return None
    if lat > cyr:
        return "EN"
    return "KZ" if kz / cyr >= KZ_SHARE_THRESHOLD else "RU"


def _round(x: float) -> float:
    return round(x + 0.0, 1)


def _overlap(a0: float, a1: float, intervals: list[tuple[float, float]]) -> float:
    return sum(max(0.0, min(a1, b1) - max(a0, b0)) for b0, b1 in intervals)


# --- основная функция -------------------------------------------------------------------
def preprocess(record: dict, settings: dict | None = None) -> Preprocessed:
    settings = settings or {}
    pause_threshold = float(settings.get("pause_threshold_s", settings.get("first_response_threshold_s", 180)))
    channel = record.get("channel", "")
    is_call = channel == "voice call"
    raw = record.get("transcript") or []

    # 1) время
    parsed = [parse_timestamp(t["timestamp"]) for t in raw]
    kinds = {k for k, _ in parsed}
    if len(kinds) > 1:
        raise ValueError(f"{record.get('conversation_id')}: смешаны форматы времени {sorted(kinds)}")
    t0 = parsed[0][1] if parsed else 0.0
    t = [v - t0 for _, v in parsed]

    # 2) события, дубли
    events = [detect_event(m["text"]) if m["speaker"] == "system" else None for m in raw]
    seen: set[tuple[str, str, str]] = set()
    dup = []
    for m in raw:
        key = (m["timestamp"], m["speaker"], m["text"])
        dup.append(key in seen)
        seen.add(key)

    def first_event(name: str) -> int | None:
        return next((i for i, e in enumerate(events) if e == name), None)

    # 3) начало работы оператора
    start_ev = first_event("agent_joined" if is_call else "escalation")
    if start_ev is None:
        start_ev = first_event("escalation") if is_call else first_event("agent_joined")
    first_op_any = next((i for i, m in enumerate(raw) if m["speaker"] == "operator" and not dup[i]), None)
    op_start = start_ev if start_ev is not None else first_op_any
    end_ev = first_event("call_ended")

    # 4) удержание и консультация (по индексам; время — для длительности)
    def intervals(start_names: set[str], end_names: set[str]) -> list[tuple[int, int | None]]:
        out, open_i = [], None
        for i, e in enumerate(events):
            if e in start_names and open_i is None:
                open_i = i
            elif e in end_names and open_i is not None:
                out.append((open_i, i))
                open_i = None
        if open_i is not None:
            out.append((open_i, None))
        return out

    holds = intervals({"hold_start"}, {"hold_end"})
    consults = intervals({"consult_start"}, {"consult_end"})
    t_end = t[-1] if t else 0.0

    def inside(i: int, ivs: list[tuple[int, int | None]]) -> bool:
        return any(a < i and (b is None or i < b) for a, b in ivs)

    def to_interval(a: int, b: int | None) -> Interval:
        return Interval(from_idx=a + 1, to_idx=None if b is None else b + 1,
                        duration_s=_round((t[b] if b is not None else t_end) - t[a]))

    hold_times = [(t[a], t[b] if b is not None else t_end) for a, b in holds]

    # 5) флаги и сегменты
    messages: list[Message] = []
    for i, m in enumerate(raw):
        flags = []
        if dup[i]:
            flags.append(DUPLICATE)
        if is_call and op_start is not None and i < op_start and m["speaker"] in ("client", "bot"):
            flags.append(IVR)
        if m["speaker"] != "client" and m["speaker"] != "system" and (inside(i, holds) or inside(i, consults)):
            flags.append(CLIENT_NOT_HEARING)
        if end_ev is not None and i > end_ev:
            flags.append(AFTER_END)
        if is_call and m["speaker"] == "client" and _DTMF.match(m["text"]):
            flags.append(DTMF)

        if AFTER_END in flags:
            seg: Segment = "after_end"
        elif inside(i, consults):
            seg = "consultation"
        elif inside(i, holds):
            seg = "hold"
        elif m["speaker"] == "operator":
            seg = "operator"
        elif op_start is None or i < op_start or (i == op_start and m["speaker"] == "system"):
            seg = "ivr" if is_call and m["speaker"] != "system" else "pre_operator"
        else:
            seg = "operator"
        messages.append(Message(
            idx=i + 1, timestamp=m["timestamp"], speaker=m["speaker"], text=m["text"],
            clean_text=clean(m["text"]), t_s=_round(t[i]), event=events[i], flags=flags, segment=seg,
            counts_for_operator=m["speaker"] == "operator" and not (NOT_COUNTED_FLAGS & set(flags)),
        ))

    # 6) аудио: от «запись запущена» (если есть) с вычетом пауз записи
    if is_call:
        rec_start = first_event("recording_started")
        anchor = t[rec_start] if rec_start is not None else 0.0
        pauses = [(t[a], t[b] if b is not None else t_end)
                  for a, b in intervals({"recording_paused"}, {"recording_resumed"})]
        for i, msg in enumerate(messages):
            off = t[i] - anchor - _overlap(anchor, t[i], pauses)
            msg.audio_offset_s = _round(max(0.0, off))

    # 7) факты
    counted_op = [m for m in messages if m.counts_for_operator]
    first_op = counted_op[0] if counted_op else None
    client_msgs = [m for m in messages if m.speaker == "client" and DUPLICATE not in m.flags and DTMF not in m.flags]

    def is_warning(text: str) -> bool:
        return bool(_WAIT_WARNING.search(_THANKS_FOR_WAITING.sub(" ", text)))

    gaps: list[Gap] = []

    def add_gap(kind, i_from: int, i_to: int, warned: bool):
        g = t[i_to] - t[i_from]
        ov = _overlap(t[i_from], t[i_to], hold_times)
        eff = max(0.0, g - ov)
        gaps.append(Gap(kind=kind, from_idx=i_from + 1, to_idx=i_to + 1, gap_s=_round(g),
                        hold_overlap_s=_round(ov), effective_s=_round(eff), warned=warned,
                        significant=eff > pause_threshold))

    first_response = None
    if first_op is not None and start_ev is not None:
        if start_ev < first_op.idx - 1:
            first_response = _round(t[first_op.idx - 1] - t[start_ev])
            add_gap("first_response", start_ev, first_op.idx - 1, warned=False)
        else:                                   # оператор ответил раньше события эскалации — ожидания не было
            first_response = 0.0

    if first_op is not None:
        pending: int | None = None
        last_op_text: str | None = None
        for i, m in enumerate(messages):
            if i < first_op.idx - 1 or AFTER_END in m.flags or DUPLICATE in m.flags:
                continue
            if m.speaker == "client" and DTMF not in m.flags:
                if pending is None:
                    pending = i
            elif m.counts_for_operator:
                if pending is not None:
                    add_gap("reply", pending, i, warned=bool(last_op_text and is_warning(last_op_text)))
                    pending = None
                last_op_text = m.text

    unwarned = [g.effective_s for g in gaps if g.significant and not g.warned]
    warned = [g.effective_s for g in gaps if g.significant and g.warned]

    # идентификация: системное событие или (в звонке) словесное подтверждение
    id_idx, id_src = None, None
    ev_id = first_event("identity_verified")
    verbal = None
    if is_call:
        masked_seen = False
        for i, m in enumerate(messages):
            if m.speaker == "client" and _MASKED.search(m.text) and m.segment == "operator":
                masked_seen = True
            elif m.counts_for_operator and masked_seen and _VERBAL_ID_CONFIRM.search(m.text):
                verbal = i
                break
    candidates = [(i, s) for i, s in ((ev_id, "system"), (verbal, "verbal")) if i is not None]
    if candidates:
        id_idx, id_src = min(candidates)

    closing = "none"
    for e in events:
        if e in CLOSING_EVENTS:
            closing = CLOSING_EVENTS[e]

    facts = Facts(
        channel=channel,
        client_language=detect_language([m.text for m in client_msgs]),
        operator_first_reply_language=detect_language([first_op.text]) if first_op else None,
        escalation_idx=None if start_ev is None else start_ev + 1,
        escalation_at=None if start_ev is None else raw[start_ev]["timestamp"],
        first_operator_idx=first_op.idx if first_op else None,
        first_response_s=first_response,
        pause_threshold_s=pause_threshold,
        gaps=gaps,
        max_unwarned_gap_s=max(unwarned, default=0.0),
        warned_gap_s=max(warned, default=0.0),
        hold_s=_round(sum(b - a for a, b in hold_times)),
        hold_intervals=[to_interval(a, b) for a, b in holds],
        consultation_intervals=[to_interval(a, b) for a, b in consults],
        identity_verified_idx=None if id_idx is None else id_idx + 1,
        identity_verified_at=None if id_idx is None else raw[id_idx]["timestamp"],
        identity_source=id_src,
        closing_event=closing,
        operator_messages=len(counted_op),
        duplicates=[m.idx for m in messages if DUPLICATE in m.flags],
    )
    return Preprocessed(conversation_id=record.get("conversation_id", ""), channel=channel,
                        language=record.get("language"), messages=messages, facts=facts)
