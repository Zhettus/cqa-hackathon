"""Настройки из .env (секреты только там)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    data_api_url: str | None
    data_api_timeout_s: float
    db_path: Path
    data_dir: Path


def _path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else ROOT / p


def get_settings() -> Settings:
    return Settings(
        data_api_url=os.getenv("DATA_API_URL") or None,
        data_api_timeout_s=float(os.getenv("DATA_API_TIMEOUT_S", "30")),
        db_path=_path(os.getenv("DB_PATH", "data/cqa.db")),
        data_dir=_path(os.getenv("DATA_DIR", "data")),
    )
