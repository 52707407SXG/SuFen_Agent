"""SuFen operating policy injected into the stable system-prompt tier."""

from __future__ import annotations

from pathlib import Path


def build_sufen_policy_block() -> str:
    policy_path = Path(__file__).with_name("system.md")
    return policy_path.read_text(encoding="utf-8").strip()
