#!/usr/bin/env python3
"""Check SuFen first-release branding and identity surfaces.

This protects the public surfaces that users and the model see first. It is
not a wholesale migration of inherited internal module names.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
import json
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - local macOS system Python fallback
    tomllib = None

REPO_ROOT = Path(__file__).resolve().parents[1]
LEGACY_BRAND = "Her" + "mes"
LEGACY_LOWER = LEGACY_BRAND.lower()
LEGACY_UPPER = LEGACY_BRAND.upper()
SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    ".pytest_cache",
    "__pycache__",
    "build",
    "dist",
}

SURFACE_PATHS = [
    "README.md",
    "AGENTS.md",
    ".env.example",
    "DISCOVERY.md",
    "RUNBOOK.md",
    "SOURCE_MAP.md",
    "TEST_REPORT.md",
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "sufen",
    "tools/sufen_mystand_tools.py",
    "tools",
    "providers",
    "plugins/web",
    "plugins/model-providers",
    "sufen_constants.py",
    "sufen_logging.py",
    "model_tools.py",
    "utils.py",
    "toolsets.py",
    "agent/prompt_builder.py",
    "agent",
    "agent/system_prompt.py",
]

BLOCKED_DELIVERY_PATHS = [
    ".github",
    "apps",
    "assets/banner.png",
    "CONTRIBUTING.md",
    "CONTRIBUTING.es.md",
    "SECURITY.md",
    "SECURITY.es.md",
    "README.es.md",
    "README.ur-pk.md",
    "README.zh-CN.md",
    "Dockerfile",
    "docker",
    "docker-compose.yml",
    "docker-compose.windows.yml",
    LEGACY_LOWER,
    f"setup-{LEGACY_LOWER}.sh",
    f"{LEGACY_LOWER}-already-has-routines.md",
    "cli-config.yaml.example",
    "datagen-config-examples",
    "locales",
    "optional-mcps",
    "optional-skills",
    "website",
    "web",
    "ui-tui",
    "tui_gateway",
    "skills",
    "plugins/platforms",
    "plugins/browser",
    "plugins/web/exa",
    "plugins/web/firecrawl",
    "plugins/web/parallel",
    "plugins/web/brave_free",
    "plugins/web/ddgs",
    "plugins/web/searxng",
    "plugins/web/xai",
    "gateway",
    f"{LEGACY_LOWER}_cli",
    "run_agent.py",
    "cli.py",
    "mcp_serve.py",
    "setup.py",
    "acp_adapter",
    "acp_registry",
    "plugins/memory",
    "plugins/kanban",
    "plugins/observability",
    "plugins/disk-cleanup",
    "plugins/security-guidance",
    "plugins/cron_providers",
    "plugins/model-providers/alibaba",
    "plugins/model-providers/alibaba-coding-plan",
    "plugins/model-providers/anthropic",
    "plugins/model-providers/arcee",
    "plugins/model-providers/azure-foundry",
    "plugins/model-providers/bedrock",
    "plugins/model-providers/copilot",
    "plugins/model-providers/copilot-acp",
    "plugins/model-providers/custom",
    "plugins/model-providers/gemini",
    "plugins/model-providers/gmi",
    "plugins/model-providers/huggingface",
    "plugins/model-providers/kilocode",
    "plugins/model-providers/kimi-coding",
    "plugins/model-providers/minimax",
    "plugins/model-providers/nous",
    "plugins/model-providers/novita",
    "plugins/model-providers/nvidia",
    "plugins/model-providers/ollama-cloud",
    "plugins/model-providers/opencode-zen",
    "plugins/model-providers/openai-codex",
    "plugins/model-providers/openrouter",
    "plugins/model-providers/qwen-oauth",
    "plugins/model-providers/stepfun",
    "plugins/model-providers/vertex",
    "plugins/model-providers/xai",
    "plugins/model-providers/xiaomi",
    "plugins/model-providers/zai",
    "scripts/install.sh",
    "scripts/install.ps1",
    "scripts/install.cmd",
    f"tests/{LEGACY_LOWER}_cli",
]

ALLOWLIST_PATHS = {
    "UPSTREAM.md",
    "LICENSE",
}

PUBLIC_SUFEN_HOME_CHECK_PATHS = {
    "README.md",
    "AGENTS.md",
    "DISCOVERY.md",
    "RUNBOOK.md",
    "SOURCE_MAP.md",
    "TEST_REPORT.md",
}

FORBIDDEN_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        re.escape(f"{LEGACY_BRAND} Agent"),
        re.escape(f"You are {LEGACY_BRAND}"),
        re.escape(f"{LEGACY_LOWER}-agent"),
        re.escape(LEGACY_UPPER),
        re.escape(LEGACY_LOWER),
        r"OpenClaw",
        r"Xiaoban",
        r"xiaoban",
        r"MYSTAND_MINER_API_KEY",
        r"MYSTAND_XIAOBAN",
    ]
]

FORBIDDEN_CANDIDATE_PATH_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"(^|/)" + re.escape(LEGACY_LOWER) + r"[^/]*",
        r"(^|/)nous[^/]*",
    ]
]


def iter_files(path: Path):
    if not path.exists():
        return
    if path.is_dir():
        for item in path.rglob("*"):
            if item.is_file() and not is_git_ignored(str(item.relative_to(REPO_ROOT))):
                yield item
    elif path.is_file() and not is_git_ignored(str(path.relative_to(REPO_ROOT))):
        yield path


def in_git_repo() -> bool:
    return (
        subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def is_git_ignored(rel: str) -> bool:
    if not in_git_repo():
        return False
    return subprocess.run(
        ["git", "check-ignore", "-q", rel],
        cwd=REPO_ROOT,
        text=True,
    ).returncode == 0


def iter_delivery_candidates() -> list[str]:
    if in_git_repo():
        return subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.splitlines()

    candidates: list[str] = []
    for item in REPO_ROOT.rglob("*"):
        if not item.is_file():
            continue
        rel_parts = item.relative_to(REPO_ROOT).parts
        if any(part in SKIP_DIRS for part in rel_parts):
            continue
        candidates.append(str(item.relative_to(REPO_ROOT)))
    return sorted(candidates)


def main() -> int:
    failures: list[str] = []
    candidates = iter_delivery_candidates()
    for rel_path in candidates:
        if rel_path in ALLOWLIST_PATHS:
            continue
        for pattern in FORBIDDEN_CANDIDATE_PATH_PATTERNS:
            if pattern.search(rel_path):
                failures.append(f"{rel_path}: forbidden inherited name in delivery candidate path")

    for rel in SURFACE_PATHS:
        if rel in ALLOWLIST_PATHS:
            continue
        for path in iter_files(REPO_ROOT / rel) or []:
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for pattern in FORBIDDEN_PATTERNS:
                if pattern.search(text):
                    failures.append(f"{path.relative_to(REPO_ROOT)}: {pattern.pattern}")
            rel_path = str(path.relative_to(REPO_ROOT))
            if (
                rel_path in PUBLIC_SUFEN_HOME_CHECK_PATHS
                and "SUFEN_HOME" in text
                and "inherited runtime compatibility" not in text
            ):
                failures.append(f"{path.relative_to(REPO_ROOT)}: SUFEN_HOME must be labeled compatibility")
    for rel in BLOCKED_DELIVERY_PATHS:
        if (REPO_ROOT / rel).exists() and not is_git_ignored(rel):
            failures.append(f"{rel}: inherited user-facing surface is not excluded from delivery")

    try:
        pyproject_text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        if tomllib is None:
            required_snippets = [
                'name = "sufen-agent"',
                'sufen = "sufen.cli:main"',
                'py-modules = ["model_tools", "toolsets", "sufen_constants", "sufen_logging", "utils"]',
                '"sufen-agent[web]"',
            ]
            for snippet in required_snippets:
                if snippet not in pyproject_text:
                    failures.append(f"pyproject.toml: missing {snippet}")
            if LEGACY_LOWER in pyproject_text.lower() or "nous" in pyproject_text.lower():
                failures.append("pyproject.toml: must not expose inherited brand names")
        else:
            pyproject = tomllib.loads(pyproject_text)
            extras = pyproject.get("project", {}).get("optional-dependencies", {})
            if set(extras) != {"all", "dev", "web"}:
                failures.append("pyproject.toml: first-release extras must be exactly all/dev/web")
            if extras.get("all") != ["sufen-agent[web]"]:
                failures.append("pyproject.toml: [all] extra must only include sufen-agent[web]")
            py_modules = pyproject.get("tool", {}).get("setuptools", {}).get("py-modules", [])
            if "sufen_constants" not in py_modules or "sufen_logging" not in py_modules:
                failures.append("pyproject.toml: SuFen compatibility modules must use sufen_* names")
            if any(LEGACY_LOWER in module.lower() or "nous" in module.lower() for module in py_modules):
                failures.append("pyproject.toml: py-modules must not expose inherited brand names")
            plugin_data = pyproject.get("tool", {}).get("setuptools", {}).get("package-data", {}).get("plugins", [])
            expected_plugin_data = [
                "web/tavily/plugin.yaml",
                "model-providers/deepseek/__init__.py",
                "model-providers/deepseek/plugin.yaml",
            ]
            if plugin_data != expected_plugin_data:
                failures.append("pyproject.toml: first-release plugin package data must be the reviewed SuFen subset")
    except Exception as exc:
        failures.append(f"pyproject.toml: could not validate SuFen extras: {exc}")

    try:
        package_json = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
        for key in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies", "workspaces"):
            if package_json.get(key):
                failures.append(f"package.json: first-release Node surface must not declare {key}")
    except Exception as exc:
        failures.append(f"package.json: could not validate Node surface: {exc}")

    try:
        package_lock = json.loads((REPO_ROOT / "package-lock.json").read_text(encoding="utf-8"))
        lock_packages = package_lock.get("packages", {})
        if set(lock_packages) != {""}:
            failures.append("package-lock.json: first-release lock must contain only the root package")
    except Exception as exc:
        failures.append(f"package-lock.json: could not validate Node lock: {exc}")

    if failures:
        print("sufen-rebrand-check failed")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("sufen-rebrand-check ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
