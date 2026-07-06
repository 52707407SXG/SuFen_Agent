"""SuFen-native session and transcript helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import re
from pathlib import Path
from typing import Any

from sufen.config import get_sufen_home

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_segment(value: str, field_name: str) -> str:
    if not value or not _SAFE_ID_RE.fullmatch(value) or ".." in value or "/" in value or "\\" in value:
        raise ValueError(f"{field_name} must be a stable ASCII id")
    return value


@dataclass
class SuFenSession:
    session_id: str
    root: Path = field(default_factory=lambda: get_sufen_home() / "sessions")

    @property
    def path(self) -> Path:
        safe_id = _safe_segment(self.session_id, "session_id")
        return self.root / f"{safe_id}.jsonl"

    def append_turn(self, *, role: str, content: Any, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        if role not in {"user", "assistant", "system", "tool"}:
            raise ValueError("role must be user, assistant, system, or tool")
        record = {
            "sessionId": self.session_id,
            "role": role,
            "content": content,
            "metadata": metadata or {},
            "createdAt": _utc_now(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        return record

    def read_transcript(self, limit: int | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows = [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if limit is not None:
            return rows[-max(0, limit):]
        return rows
