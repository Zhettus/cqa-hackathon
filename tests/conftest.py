import json
import os
from pathlib import Path

import pytest

# Тесты не зависят от .env разработчика: режим вызовов, голоса, лимиты и нормализация — фиксированные значения.
# load_dotenv не переопределяет уже заданные переменные окружения.
os.environ.update({"EVAL_MODE": "per_criterion", "VOTES": "1", "NORMALIZE_CALLS": "0", "RATE_LIMIT_RPM": "0",
                   "LLM_CONCURRENCY": "8", "LLM_TIMEOUT_S": "120", "LLM_MAX_RETRIES": "3", "LLM_EFFORT": "high"})

ROOT = Path(__file__).resolve().parent.parent


def load_records(name: str) -> dict[str, dict]:
    raw = json.loads((ROOT / "data" / name).read_text(encoding="utf-8"))
    return {r["conversation_id"]: r for r in raw["data"]["records"]}


@pytest.fixture(scope="session")
def dev_records():
    return load_records("dev.json")


@pytest.fixture(scope="session")
def edge_records():
    return load_records("edge.json")


@pytest.fixture(scope="session")
def checklist_v1():
    from backend.checklist import load_default_checklist
    return load_default_checklist()
