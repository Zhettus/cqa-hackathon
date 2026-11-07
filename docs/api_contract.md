# REST API — контракт (v0.1, реализован в `backend/main.py`)

Отличия реализации от текста ниже (состояние на 2026-11-07): `final` в ответах
`GET /api/evaluations/{id}` и решения по апелляции пока `null` — пересчёт итога с
учётом решений делается следующим этапом; `GET /api/conversations/{id}` дополнительно
отдаёт `facts`; diff версий дополнительно отдаёт `other_changed` (name/sections/settings);
`summary` результатов дополнительно содержит `failed`; объект оценки содержит
`prompt_version`, `votes`, `effort`. Тесты эндпоинтов — `tests/test_api.py`.

Контракт для параллельной работы backend и frontend. Всё под префиксом `/api`,
JSON в UTF-8, время — ISO 8601 UTC. Фронтенд раздаётся тем же FastAPI (`/ui/`),
поэтому CORS не нужен; для Dynamics 365 страница получает параметры через URL.

## 0. Общие соглашения

**Номер реплики `n`** — 1-based индекс в исходном `transcript` (как «реплика №n»
в обосновании). Используется везде: цитаты, диапазоны, ссылки в UI.
Внутри backend то же число называется `idx` (поля `*_idx` в facts).

**Ответ по критерию `answer`**: `"yes" | "no" | "na"` для binary/flag,
целое число из `scale[].value` для scale, `"na"` для scale при `na_allowed`.
Совпадает с форматом `eval/gold_*.json`.

**Пагинация**: `?limit=50&offset=0` → `{ "items": [...], "total": 123 }`.

**Ошибки** (любой код 4xx/5xx):
```json
{ "error": { "code": "checklist_version_conflict", "message": "Текст для UI на русском", "details": {} } }
```
| HTTP | когда |
|---|---|
| 400 | некорректные параметры |
| 404 | объект не найден |
| 409 | конфликт: версия чек-листа устарела, апелляция уже открыта, решение уже принято |
| 422 | невалидный чек-лист / транскрипт (в `details.errors[]` — `{path, message}`) |
| 502 | сбой источника данных или LLM-провайдера после всех повторов |

**Ссылка в UI** (в т.ч. как web resource D365):
`/ui/?conversation_id=p1-ru-001&evaluation_id=ev_…&n=12` — открыть карточку,
прокрутить к реплике 12, для звонка — перемотать плеер.

---

## 1. Наборы данных

### `POST /api/datasets/import`
Загрузка набора у организаторов по `group_id`. При недоступности API и наличии
`data/{group_id}.json` — берётся локальный файл (`source: "local"`).
Повторный импорт того же `group_id` обновляет диалоги, оценки не трогает.
```json
// запрос
{ "group_id": "DEV", "prefer": "remote" }        // prefer: "remote" | "local"
// 201
{ "group_id": "DEV", "source": "remote", "total_records": 65, "imported": 65,
  "skipped": [], "imported_at": "2026-11-07T13:20:00Z" }
```

### `POST /api/datasets/upload`
Ручная загрузка транскрипта(ов) в формате организаторов (`data.records[]`
или массив записей). Тело — тот же JSON. Ответ как у import, `source: "upload"`,
`group_id` из тела или `"UPLOAD"`.

### `GET /api/datasets`
```json
{ "items": [ { "group_id": "DEV", "source": "remote", "total_records": 65,
  "channels": {"agent chat": 60, "voice call": 5},
  "languages": {"RU": 35, "KZ": 15, "EN": 15},
  "evaluated": 40, "imported_at": "…" } ], "total": 2 }
```

### `GET /api/datasets/{group_id}` — то же, один объект.

---

## 2. Диалоги

### `GET /api/conversations`
Фильтры: `group_id`, `channel` (`agent chat|voice call`), `language`
(`RU|KZ|EN`), `status` (`not_evaluated|completed|not_evaluable|failed`),
`appeal` (`open|decided|none`), `q` (поиск по id и тексту), `limit`, `offset`.
```json
{ "items": [ {
    "conversation_id": "p1-ru-003", "group_id": "DEV",
    "channel": "agent chat", "language": "RU",
    "messages": 14, "operator_messages": 3, "has_audio": false,
    "latest_evaluation": { "id": "ev_01J…", "status": "completed",
      "checklist": {"id": "chat-quality", "version": 1},
      "total": 76.88, "final_total": 76.88, "open_appeals": 0,
      "created_at": "…" }        // null, если не оценивался
  } ], "total": 65 }
```

### `GET /api/conversations/{conversation_id}`
Транскрипт с исходными полями (не изменяются) и производными полями,
посчитанными кодом.
```json
{
  "conversation_id": "p4-ru-002", "group_id": "DEV",
  "channel": "voice call", "language": "RU",
  "audio": { "available": true, "url": "/api/conversations/p4-ru-002/audio",
             "content_type": "audio/mpeg" },          // null, если аудио нет
  "operator_start_n": 2,               // первая реплика оператора после эскалации/«Агент присоединился»; null, если нет
  "segments": [                         // интервалы, вычисленные кодом
    { "kind": "pre_operator",  "from_n": 1,  "to_n": 1 },
    { "kind": "operator",      "from_n": 2,  "to_n": 17 },
    { "kind": "hold",          "from_n": 18, "to_n": 58 },
    { "kind": "consultation",  "from_n": 19, "to_n": 46 },
    { "kind": "after_end",     "from_n": 73, "to_n": 73 }
  ],
  "messages": [ {
    "n": 3,
    "timestamp": "2026-11-03T13:49:47.2615709+00:00",  // исходник
    "speaker": "operator",                                // исходник
    "text": "…",                                          // исходник
    "clean_text": "…",                                    // нормализованный, для модели
    "t_s": 27.24,          // секунд от первой реплики диалога
    "audio_offset_s": 27.24, // позиция в плеере; null для чатов
    "segment": "operator", // pre_operator|ivr|operator|hold|consultation|after_end
    "flags": [],           // duplicate|ivr|client_not_hearing|after_end|dtmf
    "counts_for_operator": true,  // засчитывается ли реплика оператору (инвариант 9)
    "event": null          // для system: escalation|identity_verified|operator_closed|client_left|connection_lost|agent_joined|call_ended|hold_start|hold_end|consult_start|consult_end|recording|language_changed|other
  } ]
}
```

### `GET /api/conversations/{conversation_id}/audio`
Отдаёт `data/audio/{id}.<ext>`, поддерживает `Range` (нужно для перемотки).
404, если файла нет.

---

## 3. Чек-листы с версиями

Версия неизменна после создания. Изменение = новая версия. Оценка ссылается
на `{id, version}`. Формат тела — как в `docs/SPEC.md`, плюс `id` и `settings`.

**Объект чек-листа (версии)**
```json
{
  "id": "chat-quality", "version": 2, "name": "Чек-лист качества чатов",
  "created_at": "…", "author": "qa_lead", "comment": "Заменён c06",
  "is_latest": true,
  "settings": { "first_response_threshold_s": 180, "pause_threshold_s": 180, "rounding_decimals": 2 },
                                  // недостающие ключи берутся из умолчаний (pause_threshold_s = first_response_threshold_s)
  "sections": [ { "id": "start", "title": "Начало и понимание запроса" } ],
  "criteria": [ {
    "id": "c04", "section": "safety", "title": "Корректная и полная консультация",
    "type": "scale",                    // binary | scale | flag
    "weight": 20, "na_allowed": true,
    "na_condition": "…", "description": "…",
    "scale": [ { "value": 20, "description": "…" } ]   // только для scale
  } ]
}
```
Правила валидации (422): уникальные `id` критериев и секций; секция существует;
`scale` обязателен и непуст только для `scale`, значения уникальны, целые, ≥0;
`weight` ≥ 0, для `flag` = 0; `na_condition` обязателен при `na_allowed: true`;
`settings`: пороги — неотрицательные числа, `rounding_decimals` — целое 0–6; `guards` критерия —
известные факты и операторы, `value_from` разрешим, есть действие; `hints` — компилируемые регулярки.

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/api/checklists` | список: последняя версия каждого (`items[]` без `criteria`, с `criteria_count`) |
| POST | `/api/checklists` | создать новый чек-лист → версия 1 (201) |
| GET | `/api/checklists/{id}` | последняя версия целиком |
| GET | `/api/checklists/{id}/versions` | `[{version, created_at, author, comment, criteria_count}]` |
| GET | `/api/checklists/{id}/versions/{version}` | конкретная версия |
| POST | `/api/checklists/{id}/versions` | новая версия (201) |
| GET | `/api/checklists/{id}/versions/{a}/diff/{b}` | разница для UI: `{added[], removed[], changed[{criterion_id, fields[]}]}` |
| POST | `/api/checklists/validate` | проверить тело без сохранения → `{valid, errors[]}` |

`POST /api/checklists/{id}/versions` — тело: полный чек-лист плюс
`base_version`. Если `base_version` ≠ последней — 409 (кто-то сохранил раньше).
```json
{ "base_version": 1, "comment": "Заменён c06", "author": "qa_lead",
  "name": "…", "settings": {…}, "sections": […], "criteria": […] }
```

---

## 4. Оценка

### Объект оценки (неизменяемый, инвариант 6)
```json
{
  "id": "ev_01J…", "conversation_id": "p1-ru-003",
  "checklist": { "id": "chat-quality", "version": 1 },
  "status": "completed",          // completed | not_evaluable | failed
  "status_reason": null,          // для not_evaluable: "Нет реплик оператора"; для failed — текст ошибки
                                  // (любой сбой: предобработка, провайдер, валидация после повторов)
  "batch_id": null,
  "created_at": "…", "duration_ms": 8400,
  "model": { "provider": "anthropic", "name": "…" },
  "llm_call_ids": ["llm_01J…"],   // пусто для not_evaluable
  "cost": { "calls": 8, "tokens_in": 52000, "tokens_out": 3100, "usd": 0.27,
            "rejected_calls": 1 },    // сумма по llm_calls этой оценки; rejected — отклонённые валидатором
  "prompt_version": "v1-4367f2210a",   // схема промпта + хеш prompt.py и docs/rules.md
  "votes": 1, "effort": "high",         // голосов на критерий; глубина рассуждения модели
  "facts": {                      // посчитано backend/preprocess.py (инвариант 3), поля Facts
    "channel": "agent chat",
    "client_language": "RU", "operator_first_reply_language": "RU",
    "escalation_idx": 9, "escalation_at": "00:44",          // исходный timestamp
    "first_operator_idx": 10, "first_response_s": 46.0,
    "pause_threshold_s": 180,
    "gaps": [ { "kind": "reply", "from_idx": 11, "to_idx": 12, "gap_s": 515.0,
                "hold_overlap_s": 0.0, "effective_s": 515.0,
                "warned": true, "significant": true } ],     // kind: first_response | reply
    "max_unwarned_gap_s": 0.0, "warned_gap_s": 515.0,
    "hold_s": 0.0, "hold_intervals": [], "consultation_intervals": [],   // [{from_idx, to_idx, duration_s}]
    "identity_verified_idx": null, "identity_verified_at": null,
    "identity_source": null,                                // system | verbal | null
    "closing_event": "none",        // operator_closed | connection_lost | client_left | client_ended_call | none
    "operator_messages": 3, "duplicates": []
  },                              // {} для failed до предобработки
  "total": 76.88,                 // null, если нет применимых критериев с весом или not_evaluable
  "sections": [ { "id": "start", "title": "…", "score": 100.0,
                  "points": 25, "max_points": 25 } ],    // score null, если в секции всё N/A
  "flags": [ { "criterion_id": "c08", "title": "Грубые ошибки", "answer": "no" } ],
  "criteria": [ {
    "criterion_id": "c04", "section": "safety", "title": "…",
    "type": "scale", "weight": 20,
    "answer": 10,
    "points": 10.0, "applicable": true,      // applicable=false при "na"; points=null у flag
    "explanation": "Вопрос о комиссии проигнорирован…",   // на русском
    "evidence": {
      "kind": "quote",                       // quote | checked_range | na_condition
      "quotes": [ { "n": 7, "text": "…",     // для quote; text — ДОСЛОВНАЯ подстрока text реплики n
                    "model_quote": "…" } ],  // как написала модель (принято по нечёткому совпадению ≥ 0.85)
      "range": null,                         // для checked_range: { "from_n": 4, "to_n": 14 }
      "na_condition": null                   // для na_condition: текст выполненного условия
    },
    "confidence": 1.0,                       // доля голосов, совпавших с итогом (VOTES)
    "votes": [10],                           // ответы модели по голосам
    "model_answer": 10,                      // ответ модели до guards
    "guards": [ { "id": "c06_timing", "conditions": ["first_response_s = 311 > 180"],
                  "before": 10, "after": 7 } ],   // сработавшие жёсткие правила (также в explanation)
    "hints": [ { "idx": 10, "name": "transfer_to_me", "label": "…", "match": "Скиньте деньги мне" } ],
    "violation_type": null                   // для flag: none|insult|off_platform_paid_service|confidential_disclosure|other
  } ]
}
```

### `POST /api/evaluations` — оценка одного диалога
Синхронно: ответ приходит, когда оценка готова (до ~60 с). Каждый вызов —
новая оценка, предыдущие сохраняются.
```json
// запрос
{ "conversation_id": "p1-ru-003", "checklist_id": "chat-quality",
  "checklist_version": 1 }        // необязательно; по умолчанию последняя
// 201 → объект оценки (в т.ч. status=not_evaluable без вызова LLM)
```

### `GET /api/evaluations/{id}` — объект оценки плюс итог с апелляциями
```json
{ "evaluation": { … исходная оценка … },
  "appeals": [ … см. раздел 6 … ],
  "final": {                       // пересчитано scoring.py с учётом решений
    "total": 81.88,
    "sections": [ … ],
    "flags": [ … ],
    "criteria": [ { "criterion_id": "c04", "answer": 15, "points": 15.0,
                    "applicable": true, "source": "appeal",      // original | appeal
                    "appeal_id": "ap_01J…" } ]
  } }
```

### `GET /api/evaluations`
Фильтры: `conversation_id`, `batch_id`, `checklist_id`, `checklist_version`,
`status`, `limit`, `offset`. Элементы — объекты оценки без `criteria`.

---

## 5. Пакетная оценка

### `POST /api/batches` → 202
```json
// запрос: либо group_id, либо список conversation_ids
{ "group_id": "DEV", "conversation_ids": null,
  "checklist_id": "chat-quality", "checklist_version": 2,
  "only_not_evaluated": false,    // true — пропустить диалоги, уже оценённые этой версией
  "concurrency": 4 }
// 202
{ "batch_id": "bt_01J…", "status": "running", "total": 65 }
```

### `GET /api/batches/{batch_id}` — прогресс (опрос раз в 1–2 с)
```json
{ "batch_id": "bt_01J…", "status": "running",   // queued | running | completed | cancelled | failed
  "group_id": "DEV", "checklist": { "id": "chat-quality", "version": 2 },
  "created_at": "…", "finished_at": null,
  "progress": { "total": 65, "done": 30, "completed": 28, "not_evaluable": 1,
                "failed": 1, "running": 4, "queued": 31, "percent": 46.15 },
  "cost": { "tokens_in": 512000, "tokens_out": 48000, "usd": 1.92 },
  "items": [ { "conversation_id": "p1-ru-001", "status": "completed",   // queued|running|completed|not_evaluable|failed
               "evaluation_id": "ev_…", "total": 100.0, "error": null } ] }
```
`GET /api/batches` — список пакетов. `POST /api/batches/{id}/cancel` → 200,
уже готовые оценки сохраняются.

Повторное применение после замены критерия = новая версия чек-листа (раздел 3)
плюс новый пакет с этой версией.

---

## 6. Результаты

### `GET /api/results`
Сводная таблица: одна строка на диалог, берётся последняя оценка с подходящим
чек-листом. Фильтры: `group_id`, `batch_id`, `checklist_id`, `checklist_version`,
`channel`, `language`. `format=csv` — выгрузка CSV.
```json
{ "checklist": { "id": "chat-quality", "version": 1,
                 "criteria": [ { "id": "c01", "title": "…", "type": "binary" } ] },
  "items": [ { "conversation_id": "p1-ru-003", "channel": "agent chat", "language": "RU",
               "evaluation_id": "ev_…", "status": "completed",
               "total": 76.88, "final_total": 76.88,
               "answers": { "c01": "yes", "c04": 10 },
               "final_answers": { "c01": "yes", "c04": 10 },
               "flags": ["c08"],          // критерии flag с ответом "yes"
               "open_appeals": 0 } ],
  "summary": { "count": 65, "avg_total": 84.1, "not_evaluable": 1, "flagged": 3 },
  "total": 65 }
```

---

## 7. Апелляции и решения

Апелляция привязана к оценке и критерию. На пару (оценка, критерий) — одна
открытая апелляция (иначе 409). Решение одно на апелляцию, после него
апелляция закрыта.

### `POST /api/evaluations/{evaluation_id}/appeals` → 201
```json
{ "criterion_id": "c04", "comment": "Комиссия названа в реплике 9", "author": "operator_ivanov" }
```

### Объект апелляции
```json
{ "id": "ap_01J…", "evaluation_id": "ev_…", "conversation_id": "p1-ru-003",
  "criterion_id": "c04", "comment": "…", "author": "…", "created_at": "…",
  "status": "open",               // open | decided
  "original": { "answer": 10, "points": 10.0 },
  "decision": null }
```

### `POST /api/appeals/{appeal_id}/decision` → 201
```json
// запрос
{ "decision": "change",           // confirm | change
  "new_answer": 15,               // обязателен для change; проверяется по типу/шкале/na_allowed критерия
  "reason": "Комиссия названа, не уточнён рынок",   // обязателен для change
  "reviewer": "qa_lead" }
// 201 → { "appeal": {… "status": "decided",
//          "decision": { "decision": "change", "new_answer": 15, "reason": "…",
//                        "reviewer": "qa_lead", "decided_at": "…" } },
//          "final": { … как в GET /api/evaluations/{id} … } }
```

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/api/appeals` | фильтры `status`, `evaluation_id`, `conversation_id`, `criterion_id` |
| GET | `/api/appeals/{id}` | одна апелляция |

---

## 8. Логи вызовов LLM

Каждый вызов модели, включая повторы после неуспешной валидации (инвариант 5).

### `GET /api/llm-calls`
Фильтры: `evaluation_id`, `batch_id`, `conversation_id`, `status`, `limit`, `offset`.
```json
{ "items": [ {
    "id": "llm_01J…", "evaluation_id": "ev_…", "conversation_id": "p1-ru-003",
    "batch_id": null, "attempt": 1,
    "provider": "anthropic", "model": "…",
    "started_at": "…", "latency_ms": 7900,
    "tokens_in": 6400, "tokens_out": 900, "cost_usd": 0.031,
    "status": "ok",               // ok | invalid_response | provider_error
    "validation_errors": []       // [{criterion_id, code, message}], code: na_not_allowed|invalid_scale_value|quote_not_found|missing_criterion|bad_json
  } ], "total": 1 }
```

### `GET /api/llm-calls/{id}` — плюс `request` (system и user промпт,
параметры) и `raw_response` (сырой текст ответа модели).

### `GET /api/llm-calls/summary`
Фильтры как у списка. `{ "calls": 70, "tokens_in": …, "tokens_out": …,
"cost_usd": 2.1, "avg_latency_ms": 8100, "retries": 5, "by_model": [...] }`

---

## 9. Служебное

- `GET /api/health` → `{ "status": "ok" }`
- `GET /api/meta` → `{ "llm": {"provider": "…", "model": "…"},
  "data_api_configured": true, "languages": ["RU","KZ","EN"],
  "channels": ["agent chat","voice call"], "version": "0.1.0" }`

## Открытые вопросы

1. Аудио и транскрипт: `audio_offset_s` пока = время от первой реплики звонка.
   Начало mp3 может совпадать с событием «Запись и транскрибирование запущены»;
   проверим на файлах из `data/audio/`.
2. Авторизация: в прототипе нет, `author`/`reviewer` — свободная строка
   (в D365 — из контекста пользователя через URL).
