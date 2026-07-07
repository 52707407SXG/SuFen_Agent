"""Read-only human memory helpers for SuFen.

SuFen no longer owns long-term memory. The single memory folder is maintained
by Gangge and Yuan Laoshi; SuFen may only read concise human-authored context
from that root and must never create scoped per-broker/per-subject files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sufen.config import load_settings

MEMORY_FILE_SUFFIXES = {".md", ".markdown", ".txt", ".json"}
MAX_MEMORY_FILE_BYTES = 120_000


def memory_root(root: str | Path | None = None) -> Path:
    return Path(root) if root is not None else load_settings().memory_root


def iter_memory_documents(root: str | Path | None = None) -> list[dict[str, Any]]:
    base = memory_root(root)
    if not base.exists() or not base.is_dir():
        return []
    documents: list[dict[str, Any]] = []
    for path in sorted(base.iterdir(), key=lambda item: item.name.lower()):
        if not path.is_file() or path.suffix.lower() not in MEMORY_FILE_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        documents.append({
            "name": path.name,
            "path": str(path),
            "text": text[:MAX_MEMORY_FILE_BYTES],
            "truncated": len(text) > MAX_MEMORY_FILE_BYTES,
        })
    return documents


def search_human_memory(query: str = "", *, root: str | Path | None = None, limit: int = 8) -> dict[str, Any]:
    query_text = (query or "").strip().lower()
    matches: list[dict[str, Any]] = []
    for document in iter_memory_documents(root):
        text = str(document.get("text") or "")
        haystack = f"{document.get('name', '')}\n{text}".lower()
        if query_text and query_text not in haystack:
            continue
        snippet = text.strip().replace("\r\n", "\n")
        if query_text:
            index = haystack.find(query_text)
            if index > -1:
                start = max(0, index - 240)
                end = min(len(text), index + 760)
                snippet = text[start:end].strip()
        matches.append({
            "name": document.get("name", ""),
            "path": document.get("path", ""),
            "snippet": snippet[:1000],
            "truncated": bool(document.get("truncated")),
        })
        if len(matches) >= max(1, limit):
            break
    return {
        "root": str(memory_root(root)),
        "mode": "single_human_memory_root_read_only",
        "query": query,
        "matches": matches,
        "note": "SuFen can read this folder but cannot write memory or create scoped memory patches.",
    }
