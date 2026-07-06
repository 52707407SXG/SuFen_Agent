"""Small compatibility helpers for retained runtime modules.

These helpers keep SuFen's first-release wheel independent from inherited CLI
packages while preserving importability of the mature runtime chassis.
"""

from __future__ import annotations

import os
import subprocess
import sys


IS_WINDOWS = sys.platform == "win32"


def windows_hide_flags() -> int:
    """Return subprocess creation flags that hide child windows on Windows."""

    if IS_WINDOWS:
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def get_provider_request_timeout(*_args, **_kwargs) -> float:
    """Return SuFen's provider request timeout in seconds."""

    return _float_env("SUFEN_PROVIDER_REQUEST_TIMEOUT", 600.0)


def get_provider_stale_timeout(*_args, **_kwargs) -> float:
    """Return SuFen's stale-stream timeout in seconds."""

    return _float_env("SUFEN_PROVIDER_STALE_TIMEOUT", 300.0)


def load_env(*_args, **_kwargs) -> None:
    """No-op env loader; SuFen reads only SUFEN_* via sufen.config."""

    return None
