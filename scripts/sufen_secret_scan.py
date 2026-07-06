#!/usr/bin/env python3
"""High-signal secret scan for the SuFen first release."""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SKIP_DIRS = {
    ".git",
    ".local",
    ".npm-cache",
    ".pytest_cache",
    ".uv-cache",
    ".venv",
    "__pycache__",
    "node_modules",
    "sufen_agent.egg-info",
    "sufen_agent.egg-info",
}

SURFACE_PATHS = [
    "README.md",
    "AGENTS.md",
    "DISCOVERY.md",
    "RUNBOOK.md",
    "SOURCE_MAP.md",
    "TEST_REPORT.md",
    "UPSTREAM.md",
    ".env.example",
    "pyproject.toml",
    "package.json",
    "sufen",
    "tools/sufen_mystand_tools.py",
    "scripts/check_sufen_dialogue_live.py",
    "scripts/check_sufen_dialogue_policy.py",
    "scripts/sufen_rebrand_check.py",
    "scripts/sufen_secret_scan.py",
    "tests/sufen",
]

BLOCKED_FILENAMES = {
    ".env",
    "auth.json",
    "sessions.json",
}

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |)?PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{24,}"),
]

ENV_EXAMPLE_ALLOWED_EMPTY = {
    "SUFEN_API_KEY",
    "SUFEN_BASE_URL",
    "SUFEN_TAVILY_API_KEY",
}


def iter_all_files():
    for path in REPO_ROOT.rglob("*"):
        if any(part in SKIP_DIRS for part in path.relative_to(REPO_ROOT).parts):
            continue
        if path.is_file():
            yield path


def iter_surface_files():
    for rel in SURFACE_PATHS:
        path = REPO_ROOT / rel
        if path.is_dir():
            for item in path.rglob("*"):
                if item.is_file():
                    yield item
        elif path.is_file():
            yield path


def check_env_example(failures: list[str]) -> None:
    env_example = REPO_ROOT / ".env.example"
    if not env_example.exists():
        failures.append(".env.example is missing")
        return
    for line_no, line in enumerate(env_example.read_text(encoding="utf-8").splitlines(), start=1):
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name in ENV_EXAMPLE_ALLOWED_EMPTY and value.strip():
            failures.append(f".env.example:{line_no}: {name} must be blank in the template")


def main() -> int:
    failures: list[str] = []
    for path in iter_all_files():
        rel = path.relative_to(REPO_ROOT)
        if path.name in BLOCKED_FILENAMES:
            failures.append(f"{rel}: runtime secret/state file must not be committed")
    for path in iter_surface_files():
        rel = path.relative_to(REPO_ROOT)
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                failures.append(f"{rel}: possible secret matched {pattern.pattern}")
    check_env_example(failures)
    if failures:
        print("sufen-secret-scan failed")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("sufen-secret-scan ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
