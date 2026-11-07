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
- Карточка диалога: `/ui/?conversation_id=<id>&evaluation_id=<ev_id>` (обоснования, цитаты по `n`,
  для звонков — `text_clean` и плеер по `audio_offset_s`).
- Логи и стоимость: `curl 'localhost:8000/api/llm-calls/summary?batch_id=bt_…'`.

## 4. Если что-то пошло не так

| Симптом | Действие |
|---|---|
| `502 data_source_error` при импорте | проверить `DATA_API_URL`/сеть; сохранить JSON организаторов в `data/<group_id>.json` и импортировать с `prefer: local` |
| пакет `failed` с ошибкой аутентификации | проверить `LLM_API_KEY`, перезапустить сервер, создать пакет заново (`only_not_evaluated: true`) |
| отдельные `failed` в пакете | `POST /api/evaluations` по диалогу; причина — в `items[].error` и `GET /api/llm-calls?conversation_id=…` |
| `not_evaluable` | в диалоге нет засчитываемых реплик оператора (R1/R11) — итог `null`, это ожидаемое поведение |
| нужно поменять критерий | `POST /api/checklists/chat-quality/versions` (новая версия) → новый пакет с `checklist_version` |
| мало времени | `VOTES=3` (×0.6 времени); `NORMALIZE_CALLS=0` экономит ~1 вызов на звонок |

Стоимость: ≈ $0.10 на диалог за голос (claude-opus-5-5); 10 диалогов при VOTES=5 ≈ $5.
