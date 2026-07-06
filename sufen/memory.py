"""Scoped memory storage helpers for SuFen.

Memory is scoped by company, operator, subject type, and subject id. The path
uses stable ids only; names in Chinese or other display labels belong inside
metadata, never in the directory structure.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from pathlib import Path
from typing import Any

from sufen.config import load_settings

SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

DEFAULT_MEMORY = {
    "scope": {},
    "metadata": {},
    "businessFacts": [],
    "strategyObservations": [],
    "brokerAdaptation": [],
    "openQuestions": [],
    "lastSummaries": [],
    "memoryIndexText": "",
    "sourceRefs": [],
    "confidence": 0.0,
    "createdAt": None,
    "updatedAt": None,
}


def _safe_segment(value: str, field: str) -> str:
    value = (value or "").strip()
    if not value or not SAFE_SEGMENT_RE.fullmatch(value):
        raise ValueError(f"{field} must be a stable ASCII id segment")
    return value


def memory_path(
    *,
    company_id: str,
    operator_user_id: str,
    subject_type: str,
    subject_id: str,
    root: str | Path | None = None,
    admin: bool = False,
) -> Path:
    base = Path(root) if root is not None else load_settings().memory_root
    company = _safe_segment(company_id, "company_id")
    operator = _safe_segment(operator_user_id, "operator_user_id")
    stype = _safe_segment(subject_type, "subject_type")
    sid = _safe_segment(subject_id, "subject_id")
    if admin:
        return base / company / "admin" / operator / "subjects" / stype / sid / "memory.json"
    return base / company / "operators" / operator / "subjects" / stype / sid / "memory.json"


def load_memory(path: Path) -> dict[str, Any]:
    if not path.exists():
        return dict(DEFAULT_MEMORY)
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    merged = dict(DEFAULT_MEMORY)
    if isinstance(data, dict):
        merged.update(data)
    return merged


def draft_memory_patch(scope: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "businessFacts",
        "strategyObservations",
        "brokerAdaptation",
        "openQuestions",
        "lastSummary",
        "memoryIndexText",
        "sourceRefs",
        "confidence",
    }
    clean = {key: value for key, value in patch.items() if key in allowed}
    return {
        "scope": dict(scope),
        "patch": clean,
        "draftOnly": True,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "note": "SuFen returns memoryPatch drafts only. My Stand reviews and writes them.",
    }
