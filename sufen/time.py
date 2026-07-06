"""Timezone-aware clock for SuFen-Agent."""

from __future__ import annotations

from datetime import datetime
import logging
import os
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - SuFen requires Python 3.11+
    ZoneInfo = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)

_cached_tz: Optional[ZoneInfo] = None
_cached_tz_name: str = ""
_cache_resolved = False


def _resolve_timezone_name() -> str:
    """Read the configured IANA timezone string, if one is set."""

    return os.getenv("SUFEN_TIMEZONE", "").strip()


def _get_zoneinfo(name: str) -> Optional[ZoneInfo]:
    if not name or ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except Exception as exc:
        logger.warning("Invalid SUFEN_TIMEZONE %r: %s. Falling back to server local time.", name, exc)
        return None


def get_timezone() -> Optional[ZoneInfo]:
    """Return SuFen's configured timezone, or None for server-local time."""

    global _cached_tz, _cached_tz_name, _cache_resolved
    if not _cache_resolved:
        _cached_tz_name = _resolve_timezone_name()
        _cached_tz = _get_zoneinfo(_cached_tz_name)
        _cache_resolved = True
    return _cached_tz


def reset_cache() -> None:
    """Clear cached timezone resolution for tests and config reloads."""

    global _cached_tz, _cached_tz_name, _cache_resolved
    _cached_tz = None
    _cached_tz_name = ""
    _cache_resolved = False


def now() -> datetime:
    """Return the current timezone-aware SuFen clock time."""

    tz = get_timezone()
    if tz is not None:
        return datetime.now(tz)
    return datetime.now().astimezone()
