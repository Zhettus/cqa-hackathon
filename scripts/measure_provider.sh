#!/bin/bash
# Замер облачного OpenAI-совместимого провайдера (по умолчанию Gemini) на edge (12) + tune15, VOTES=1.
# Отдельная БД (по умолчанию data/cqa_gemini.db) — оценки Opus в data/cqa.db не трогаются.
#   MODE=per_dialog|per_criterion  VERSION=1|2  MODEL=gemini-3.8-flash  PARALLEL=3  RATE_LIMIT_RPM=5
#   SETS="edge tune15"  scripts/measure_provider.sh
# Ключ берётся из .env: GEMINI_API_KEY | GEMINI_API (или уже выставленный LLM_API_KEY при CLOUD_KEY=1).
set -u
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
DB=${DB:-data/cqa_gemini.db}; LOG=${LOG:-tmp/measure_gemini.log}
MODE=${MODE:-per_dialog}; VERSION=${VERSION:-1}; MODEL=${MODEL:-gemini-3.8-flash}; PARALLEL=${PARALLEL:-3}
SETS=${SETS:-"edge tune15"}
TUNE15="p1-ru-001 p1-ru-003 p1-ru-005 p1-ru-007 p1-ru-018 p1-ru-022 p1-ru-029 p1-kz-001 p1-kz-008 p1-kz-015 p1-en-006 p1-en-013 p1-en-014 p4-ru-002 p4-ru-003"
export LLM_PROVIDER=openai LLM_MODEL=$MODEL
export LLM_BASE_URL=${CLOUD_BASE_URL:-https://generativelanguage.googleapis.com/v1beta/openai}
export LLM_API_KEY=${GEMINI_API_KEY:-${GEMINI_API:?нет ключа GEMINI_API_KEY/GEMINI_API в .env}}
export LLM_TIMEOUT_S=${LLM_TIMEOUT_S:-300} LLM_CONCURRENCY=$PARALLEL LLM_MAX_RETRIES=2 VOTES=1
export RATE_LIMIT_RPM=${RATE_LIMIT_RPM:-5} RATE_LIMIT_BACKOFF_S=${RATE_LIMIT_BACKOFF_S:-20}
mkdir -p "$(dirname "$LOG")"
# кандидат v2 (со справочником) в БД замера, если его там ещё нет
.venv/bin/python - "$DB" <<'PY'
import json, sys
from backend import db
conn = db.connect(sys.argv[1]); db.init_db(conn)
c = json.load(open("backend/checklists/candidates/chat-quality_v2_reference.json", encoding="utf-8"))
have = {v["version"] for v in db.list_checklist_versions(conn, c["id"])}
if 2 not in have:
    db.insert_checklist_version(conn, c, author="knowledge", comment="кандидат v2: reference для c04", version=2)
    print("v2 inserted into", sys.argv[1])
conn.close()
PY
{
  for SET in $SETS; do
    echo "=== $(date +%F\ %T) mode=$MODE v$VERSION model=$MODEL set=$SET rpm=$RATE_LIMIT_RPM parallel=$PARALLEL ==="
    case $SET in
      edge)   .venv/bin/python -m eval.run --db "$DB" --gold eval/edge_cases.json --mode "$MODE" --version "$VERSION" --parallel "$PARALLEL" ;;
      tune15) .venv/bin/python -m eval.run --db "$DB" --gold eval/gold_tune.json --mode "$MODE" --version "$VERSION" --parallel "$PARALLEL" --ids $TUNE15 ;;
      tune)   .venv/bin/python -m eval.run --db "$DB" --gold eval/gold_tune.json --mode "$MODE" --version "$VERSION" --parallel "$PARALLEL" ;;
    esac
    echo "=== $(date +%F\ %T) exit=$? ==="
  done
} >> "$LOG" 2>&1
