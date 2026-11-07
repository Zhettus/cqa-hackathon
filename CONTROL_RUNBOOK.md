# Контрольный набор (23-й час): порядок действий

Цель: получить оценки 10 новых диалогов по `group_id` организаторов в сжатые сроки, без ручных шагов
в коде. Всё делается через REST API работающего сервера (или `eval.run`, если нужен отчёт).

## Зафиксированная конфигурация (сверить по `GET /api/meta` перед шагом 1)

| Параметр | Значение |
|---|---|
| модель / effort | `claude-opus-5-5` / `high` (`meta.llm`) |
| версия промпта | **`v1-4ed6b8ce74`** (`meta.prompt_version`; иначе — кто-то менял `backend/prompt.py` или `docs/rules.md`) |
| чек-лист | **`chat-quality` v1** (`meta.checklists == [{"id":"chat-quality","version":1}]`); кандидат v2 со справочником не активирован — см. `backend/checklists/candidates/README.md` |
| голоса | **5** (`meta.votes`) |
| нормализация звонков в оценке | **выключена** (`meta.normalize_calls == false`) |

Точность этой конфигурации на эталоне (`--votes 3`): tune 425/440 = 96.6 %, edge 87/88 (`docs/STATUS.md` §3).
Ничего не подгружается на лету: промпт и правила — файлы репозитория на зафиксированном коммите, чек-лист — из БД
(засеян из `backend/checklists/default_v1.json`), модель и параметры — из `.env`.

## 0. Заранее (за час)

1. `.env`: `LLM_API_KEY` действителен, `DATA_API_URL` задан; настройки прогона:
   ```
   VOTES=5
   NORMALIZE_CALLS=0
   LLM_CONCURRENCY=8
   ```
2. Перезапустить сервер: `.venv/bin/uvicorn backend.main:app --port 8000`.
3. Проверить: `curl localhost:8000/api/health`, `curl localhost:8000/api/meta` (провайдер, модель,
   `data_api_configured: true`).
4. Убедиться, что текущий чек-лист — нужная версия: `curl localhost:8000/api/checklists/chat-quality`.
5. Прогрев: оценить один диалог DEV (`POST /api/evaluations`) — проверка ключа и модели (~$0.5 при VOTES=5).

## 1. Импорт

```
curl -X POST localhost:8000/api/datasets/import -H 'content-type: application/json' \
     -d '{"group_id":"<GROUP_ID>","prefer":"remote"}'
```
Ответ: `source: "remote"`, `imported: 10`. Если API организаторов недоступен и файл сохранён локально как
`data/<group_id>.json` — тот же запрос с `"prefer":"local"`. Ручная загрузка JSON: `POST /api/datasets/upload`.

Проверка: `curl 'localhost:8000/api/conversations?group_id=<GROUP_ID>'` — 10 диалогов, у звонков
`has_audio` (аудио скачивается отдельно `python scripts/download_audio.py data/<group_id>.json`).

## 2. Пакетная оценка

```
curl -X POST localhost:8000/api/batches -H 'content-type: application/json' \
     -d '{"group_id":"<GROUP_ID>","checklist_id":"chat-quality","concurrency":4}'
→ {"batch_id":"bt_…","status":"running","total":10}
```
Прогресс: `curl localhost:8000/api/batches/bt_…` раз в 2–5 с (`progress.percent`, `items[].status`).
Сбой одного диалога не останавливает пакет (`failed` с причиной в `items[].error`); повтор —
`POST /api/evaluations {"conversation_id": "…"}`.

Ориентир по времени (замер на 10 диалогах DEV, VOTES=5, NORMALIZE_CALLS=0, concurrency 4):
**замер не выполнен — баланс API исчерпан 2026-11-08 02:17.** Оценка по измеренным прогонам:
≈ 3–5 минут и ≈ $5 на 10 диалогов (при `--votes 3` 55 диалогов заняли 630 с при 4 параллельных).
Замер после пополнения: перезапустить сервер с настройками §0, затем
```
BID=$(curl -s -X POST localhost:8000/api/batches -H 'content-type: application/json' \
  -d '{"conversation_ids":["p1-ru-001","p1-ru-002","p1-ru-004","p1-ru-005","p1-ru-006","p1-ru-008","p1-kz-001","p1-en-001","p1-en-003","p4-ru-002"],"concurrency":4}' \
  | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["batch_id"])')
time (until curl -s localhost:8000/api/batches/$BID | grep -q '"status": *"completed"'; do sleep 3; done)
```
и записать время сюда и в `docs/STATUS.md` §6.

## 3. Результаты

- Таблица: `curl 'localhost:8000/api/results?group_id=<GROUP_ID>'`; CSV для сдачи:
  `curl 'localhost:8000/api/results?group_id=<GROUP_ID>&format=csv' -o results.csv`.
- Карточка диалога: `http://localhost:8000/?view=dialog&conversation=<id>` (обоснования, цитаты по `n`,
  для звонков — очищенный текст и плеер по `audio_offset_s`); список — `/#dialogs`, результаты — `/#analytics`.
- Логи и стоимость: `curl 'localhost:8000/api/llm-calls/summary?batch_id=bt_…'`.

## 3а. Если облачная модель недоступна (баланс, ключ, лимиты)

Сообщение пакета вида «У провайдера закончился баланс/квота (биллинг)…» или «Провайдер отклонил ключ…» —
пакет остановлен, оценённые диалоги сохранены. Переключение (только `.env`, перезапуск сервера):

1. Бесплатная облачная (если есть ключ): `LLM_PROVIDER=openai`, `LLM_BASE_URL`/`LLM_MODEL`/`LLM_API_KEY`
   из `.env.example` (Gemini, Groq или OpenRouter), `RATE_LIMIT_RPM` под тариф, `EVAL_MODE=per_dialog`,
   `VOTES=1`. Точность не измерена (ключа не было).
2. Локальная Ollama (`ollama/README.md`): `LLM_PROVIDER=openai  LLM_BASE_URL=http://localhost:11434/v1
   LLM_MODEL=cqa-qwen14b  LLM_API_KEY=ollama  LLM_CONCURRENCY=1  LLM_TIMEOUT_S=900  EVAL_MODE=per_dialog  VOTES=1`;
   пакет с `concurrency: 1`. Измерено: ≈ 181 с на диалог → 10 диалогов ≈ 30 мин, $0; точность ≈ 70 % по критериям против 97 % у Opus (`docs/STATUS.md` §4а).
3. Затем новый пакет: `POST /api/batches {"group_id":"<GROUP_ID>","only_not_evaluated":true,"concurrency":1}` —
   оценит только то, что не успела облачная модель (версия чек-листа та же).

При 429 (лимит запросов) ничего делать не нужно: пакет ждёт `RATE_LIMIT_BACKOFF_S × 2^n` и повторяет сам
(статус элемента остаётся `running`, причина ожидания — в `items[].error`).

## 4. Если что-то пошло не так

| Симптом | Действие |
|---|---|
| `502 data_source_error` при импорте | проверить `DATA_API_URL`/сеть; сохранить JSON организаторов в `data/<group_id>.json` и импортировать с `prefer: local` |
| пакет `failed` с ошибкой аутентификации или биллинга | диалоги со статусом `provider_error` не оценены (записей оценки нет); проверить `LLM_API_KEY`/баланс или переключить провайдера (§3а), перезапустить сервер, новый пакет с `only_not_evaluated: true` |
| отдельные `failed` в пакете | `POST /api/evaluations` по диалогу; причина — в `items[].error` и `GET /api/llm-calls?conversation_id=…` |
| `not_evaluable` | в диалоге нет засчитываемых реплик оператора (R1/R11) — итог `null`, это ожидаемое поведение |
| нужно поменять критерий | `POST /api/checklists/chat-quality/versions` (новая версия) → новый пакет с `checklist_version` |
| мало времени | `VOTES=3` (×0.6 времени); `NORMALIZE_CALLS=0` экономит ~1 вызов на звонок |

Стоимость: ≈ $0.10 на диалог за голос (claude-opus-5-5); 10 диалогов при VOTES=5 ≈ $5.
