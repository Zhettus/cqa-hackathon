"""scripts/fetch_group.py: исходный ответ API сохраняется байт в байт, копия — как локальный набор для импорта."""
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from fetch_group import fetch  # noqa: E402

from backend.data_source import DataSourceError  # noqa: E402


def test_fetch_saves_raw_and_local_copy(tmp_path, edge_records):
    body = json.dumps({"error": None, "isSuccess": True,
                       "data": {"dataset_group_id": "PROD1", "records": list(edge_records.values())[:3]}},
                      ensure_ascii=False).encode("utf-8")
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    raw = tmp_path / "prod_raw.json"
    records = fetch("PROD1", "https://api.example/datasets", 10, raw, tmp_path, client)
    assert seen["url"] == "https://api.example/datasets?group_id=PROD1"
    assert raw.read_bytes() == body and (tmp_path / "PROD1.json").read_bytes() == body
    assert [r["conversation_id"] for r in records] == [r["conversation_id"] for r in list(edge_records.values())[:3]]


def test_fetch_keeps_error_body_and_fails(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom")))
    raw = tmp_path / "raw.json"
    with pytest.raises(DataSourceError, match="HTTP 500"):
        fetch("PROD1", "https://api.example/datasets", 10, raw, tmp_path, client)
    assert raw.read_text() == "boom" and not (tmp_path / "PROD1.json").exists()
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="not json")))
    with pytest.raises(DataSourceError, match="не JSON"):
        fetch("PROD1", "https://api.example/datasets", 10, raw, tmp_path, client)
