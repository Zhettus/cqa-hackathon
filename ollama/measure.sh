#!/bin/bash
# Замер локальной модели: edge целиком + 15 диалогов tune (все языки, 2 звонка), VOTES=1.
# Отдельная БД data/cqa_local.db — оценки Opus в data/cqa.db не трогаются.
set -u
cd "$(dirname "$0")/.."
LOG=${LOG:-ollama/measure.log}
MODE=${MODE:-per_dialog}
MODEL=${MODEL:-cqa-qwen14b}
TUNE15="p1-ru-001 p1-ru-003 p1-ru-005 p1-ru-007 p1-ru-018 p1-ru-022 p1-ru-029 p1-kz-001 p1-kz-008 p1-kz-015 p1-en-006 p1-en-013 p1-en-014 p4-ru-002 p4-ru-003"
export LLM_PROVIDER=openai LLM_BASE_URL=http://localhost:11434/v1 LLM_MODEL=$MODEL LLM_API_KEY=ollama
export LLM_TIMEOUT_S=900 LLM_CONCURRENCY=1 LLM_MAX_RETRIES=1 VOTES=1
{
  echo "=== $(date +%F\ %T) mode=$MODE model=$MODEL edge ==="
  .venv/bin/python -m eval.run --db data/cqa_local.db --gold eval/edge_cases.json --mode $MODE --parallel 1
  echo "=== $(date +%F\ %T) mode=$MODE model=$MODEL tune15 ==="
  .venv/bin/python -m eval.run --db data/cqa_local.db --gold eval/gold_tune.json --mode $MODE --parallel 1 --ids $TUNE15
  echo "=== $(date +%F\ %T) done mode=$MODE ==="
} >> "$LOG" 2>&1
