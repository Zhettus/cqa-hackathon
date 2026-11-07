import json
from pathlib import Path

import pytest

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
