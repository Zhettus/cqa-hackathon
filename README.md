# CQA — Conversation Quality Analytics

Прототип оценки работы оператора контакт-центра в чатах и звонках по настраиваемому чек-листу:
баллы, обоснования, реплики-доказательства, апелляции, пакетная оценка набора. Хакатон Freedom Broker.

- Задание — `docs/SPEC.md`; трактовки критериев — `docs/rules.md` (входят в промпт модели).
- Контракт REST API — `docs/api_contract.md`; состояние проекта и метрики — `docs/STATUS.md`.
- Прогон контрольного набора — `CONTROL_RUNBOOK.md`.

## Запуск

```
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # LLM_API_KEY=… (Anthropic) или LLM_PROVIDER=openai + LLM_BASE_URL (Ollama/vLLM)
.venv/bin/python -m pytest -q   # 232 passed
.venv/bin/uvicorn backend.main:app --reload     # http://localhost:8000 → /ui/ (фронтенд), /docs (OpenAPI)
```

Загрузка набора организаторов по `group_id` (при недоступности API — `data/{group_id}.json`):

```
curl -X POST localhost:8000/api/datasets/import -H 'content-type: application/json' -d '{"group_id":"DEV"}'
curl -X POST localhost:8000/api/batches -H 'content-type: application/json' -d '{"group_id":"DEV","checklist_id":"chat-quality"}'
curl 'localhost:8000/api/results?group_id=DEV&format=csv'
```

## Как устроено

| Слой | Файлы | Что делает |
|---|---|---|
| Данные | `backend/data_source.py`, `backend/db.py` | API организаторов → SQLite; транскрипты хранятся как получены; оценки, версии чек-листов, логи, апелляции неизменяемы (триггеры) |
| Предобработка | `backend/preprocess.py` | нумерация реплик, время (MM:SS / ISO), флаги (IVR, удержание, после завершения, дубли, DTMF), факты: первая реакция, паузы с предупреждением, удержание, идентификация, завершение, позиция в аудио |
| Нормализация звонков | `backend/normalize.py` | модель-корректор ASR: `text_clean` и `asr_uncertain` на реплику, кэш; оригинал не меняется; в оценке — по `NORMALIZE_CALLS` |
| Оценка | `backend/prompt.py`, `backend/evaluator.py`, `backend/validator.py`, `backend/guards.py`, `backend/hints.py` | промпт из конфига критерия + `docs/rules.md`; один вызов на критерий, параллельно; валидация ответа (N/A, ступени, цитата — дословная подстрока оригинала) с повтором; жёсткие правила по фактам (guards); голосование `VOTES`; каждый вызов в логе со стоимостью |
| Баллы | `backend/scoring.py` | единственное место расчёта: N/A с перераспределением, шкалы, секции, флаги, Decimal ROUND_HALF_UP |
| Апелляции | `backend/appeals.py` | open → in_review → confirmed \| changed, журнал; `final` пересчитывается по решениям, исходная оценка неизменна |
| API | `backend/main.py`, `backend/batches.py` | REST по контракту, пакеты с прогрессом и отменой, статика `frontend/` |
| Проверка качества | `eval/run.py`, `eval/compare.py` | сравнение с эталоном `eval/gold_tune.json`, `eval/edge_cases.json`; отчёты в `eval/reports/` |

Чек-лист — данные (`backend/checklists/default_v1.json`, версии в БД). Новый критерий любого типа
(да/нет, шкала, флаг) с N/A, guards и подсказками работает без правки кода.

## Точность на эталоне

Модель claude-opus-5-5 (effort high). Эталон размечен командой (статус черновика), holdout
не использовался при настройке.

| Набор | Чек-лист v1, `--votes 3` | v1 + справочник c04 (кандидат v2), `--votes 3` |
|---|---|---|
| tune (55 диалогов, 440 критериев) | **425/440 = 96.6 %**, MAE итога 2.17, точных итогов 43/55, единогласно 433/440 | 413/424 = 97.4 % на 53 оценённых диалогах (2 не оценены — закончился баланс API), MAE 1.60 |
| edge (12 случаев, 88 критериев) | **87/88**, пройдено 11/12 | **88/88**, пройдено 12/12 |

По критериям на tune (v1): c01 55/55, c02 52/55, c03 54/55, c04 49/55, c05 55/55, c06 50/55, c07 55/55, c08 55/55.
Нормализация звонков моделью-корректором на результат не влияет (звонки 22/24 с ней и без) и в оценке выключена.

Как читать: 1 голос даёт разброс до ±6 совпадений из 440 между прогонами на одинаковом промпте;
итоговые цифры сняты с `--votes 3` (ответ по критерию — голосование трёх вызовов).
Детали, история прогонов и открытые вопросы к разметке — `docs/STATUS.md`.
