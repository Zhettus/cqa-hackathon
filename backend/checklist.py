"""Проверка структуры чек-листа (правила из docs/api_contract.md, раздел 3)."""
from __future__ import annotations

import json
from pathlib import Path

from backend.guards import validate_guards
from backend.hints import validate_hints

CHECKLISTS_DIR = Path(__file__).parent / "checklists"
DEFAULT_CHECKLIST_PATH = CHECKLISTS_DIR / "default_v1.json"
CRITERION_TYPES = {"binary", "scale", "flag"}
MAX_REFERENCE_CHARS = 20000

# Умолчания settings: недостающие ключи версии чек-листа берутся отсюда.
# pause_threshold_s по умолчанию равен first_response_threshold_s (как в preprocess).
DEFAULT_SETTINGS = {"first_response_threshold_s": 180, "rounding_decimals": 2}
THRESHOLD_KEYS = ("first_response_threshold_s", "pause_threshold_s")
MAX_ROUNDING_DECIMALS = 6


def load_default_checklist() -> dict:
    return json.loads(DEFAULT_CHECKLIST_PATH.read_text(encoding="utf-8"))


def load_checklist_files() -> list[dict]:
    """Все версии чек-листа из backend/checklists/default_v*.json по возрастанию версии (засеваются при старте)."""
    files = sorted(CHECKLISTS_DIR.glob("default_v*.json"), key=lambda f: int(f.stem.split("_v")[1]))
    return [json.loads(f.read_text(encoding="utf-8")) for f in files]


def effective_settings(checklist: dict) -> dict:
    """settings версии чек-листа, дополненные умолчаниями. Единственное место, где они сливаются."""
    s = {**DEFAULT_SETTINGS, **(checklist.get("settings") or {})}
    s.setdefault("pause_threshold_s", s["first_response_threshold_s"])
    return s


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_settings(settings) -> list[dict]:
    errs = []
    if settings is None:
        return errs
    if not isinstance(settings, dict):
        return [{"path": "settings", "message": "settings — объект"}]
    for key in THRESHOLD_KEYS:
        if key in settings and (not _is_number(settings[key]) or settings[key] < 0):
            errs.append({"path": f"settings.{key}", "message": "неотрицательное число секунд"})
    rd = settings.get("rounding_decimals")
    if rd is not None and (isinstance(rd, bool) or not isinstance(rd, int) or not 0 <= rd <= MAX_ROUNDING_DECIMALS):
        errs.append({"path": "settings.rounding_decimals", "message": f"целое от 0 до {MAX_ROUNDING_DECIMALS}"})
    return errs


def validate_checklist(cl: dict) -> list[dict]:
    """Список ошибок вида {path, message}; пустой — чек-лист корректен."""
    errs: list[dict] = []

    def err(path: str, msg: str):
        errs.append({"path": path, "message": msg})

    if not isinstance(cl.get("id"), str) or not cl.get("id"):
        err("id", "нужен строковый id")
    if not cl.get("name"):
        err("name", "нужно название")
    sections = cl.get("sections")
    criteria = cl.get("criteria")
    if not isinstance(sections, list) or not sections:
        err("sections", "нужна хотя бы одна секция")
        sections = []
    if not isinstance(criteria, list) or not criteria:
        err("criteria", "нужен хотя бы один критерий")
        criteria = []
    sec_ids = [s.get("id") for s in sections]
    if len(set(sec_ids)) != len(sec_ids):
        err("sections", "id секций должны быть уникальны")
    crit_ids = [c.get("id") for c in criteria]
    if len(set(crit_ids)) != len(crit_ids):
        err("criteria", "id критериев должны быть уникальны")

    for i, c in enumerate(criteria):
        p = f"criteria[{i}]"
        if not c.get("id"):
            err(f"{p}.id", "нужен id")
        if c.get("section") not in sec_ids:
            err(f"{p}.section", f"секция {c.get('section')!r} не существует")
        if not c.get("title"):
            err(f"{p}.title", "нужно название")
        ctype = c.get("type")
        if ctype not in CRITERION_TYPES:
            err(f"{p}.type", f"тип должен быть одним из {sorted(CRITERION_TYPES)}")
        w = c.get("weight")
        if isinstance(w, bool) or not isinstance(w, (int, float)) or w < 0:
            err(f"{p}.weight", "вес — неотрицательное число")
        elif ctype == "flag" and w != 0:
            err(f"{p}.weight", "у критерия flag вес 0")
        if not isinstance(c.get("na_allowed"), bool):
            err(f"{p}.na_allowed", "нужно true/false")
        elif c["na_allowed"] and not c.get("na_condition"):
            err(f"{p}.na_condition", "при na_allowed нужно условие N/A")
        scale = c.get("scale")
        if ctype == "scale":
            if not isinstance(scale, list) or not scale:
                err(f"{p}.scale", "для scale нужна непустая шкала")
            else:
                vals = [s.get("value") for s in scale]
                if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in vals):
                    err(f"{p}.scale", "ступени шкалы — целые числа ≥ 0")
                elif len(set(vals)) != len(vals):
                    err(f"{p}.scale", "ступени шкалы должны быть уникальны")
                elif max(vals) == 0:
                    err(f"{p}.scale", "максимальная ступень должна быть > 0")
        elif scale:
            err(f"{p}.scale", "шкала допустима только для type=scale")
        ref = c.get("reference")
        if ref is not None and (not isinstance(ref, str) or len(ref) > MAX_REFERENCE_CHARS):
            err(f"{p}.reference", f"справочник — текст до {MAX_REFERENCE_CHARS} символов")
    settings_errs = validate_settings(cl.get("settings"))
    errs += settings_errs
    if not settings_errs:
        eff = effective_settings(cl)
        for i, c in enumerate(criteria):
            p = f"criteria[{i}]"
            for msg in validate_guards(c, eff):
                err(f"{p}.guards", msg)
            for msg in validate_hints(c):
                err(f"{p}.hints", msg)
    return errs
