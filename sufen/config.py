"""Configuration helpers for SuFen-Agent.

The first-release rule is strict: SuFen reads only SUFEN_* credentials. It does
not fall back to Miner, generic My Stand, or inherited runtime keys.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


DEFAULT_HOME = Path("/var/lib/sufen-agent")
DEFAULT_MEMORY_ROOT = DEFAULT_HOME / "memory"
REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class SuFenSettings:
    provider: str
    model: str
    api_key: str
    base_url: str
    fake_provider: bool
    delegation_hmac_secret: str
    tavily_api_key: str
    memory_root: Path
    bind_host: str
    port: int
    home: Path


def _candidate_env_files() -> list[Path]:
    candidates = [Path.cwd() / ".env", REPO_ROOT / ".env"]
    unique: list[Path] = []
    for path in candidates:
        if path not in unique:
            unique.append(path)
    return unique


def _unquote_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1].strip()
    return value


def _read_sufen_dotenv_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for path in _candidate_env_files():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            key, raw_value = line.split("=", 1)
            key = key.strip()
            if not key.startswith("SUFEN_"):
                continue
            values.setdefault(key, _unquote_env_value(raw_value))
    return values


def _env(name: str, default: str = "") -> str:
    process_value = os.environ.get(name)
    if process_value is not None:
        return process_value.strip()
    return _read_sufen_dotenv_values().get(name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name, "1" if default else "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def get_sufen_home() -> Path:
    return Path(_env("SUFEN_HOME", str(DEFAULT_HOME))).expanduser()


def load_settings() -> SuFenSettings:
    raw_port = _env("SUFEN_PORT", "8791")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError("SUFEN_PORT must be an integer") from exc

    home = get_sufen_home()
    return SuFenSettings(
        provider=_env("SUFEN_PROVIDER", "deepseek"),
        model=_env("SUFEN_MODEL", "deepseek-v4-pro"),
        api_key=_env("SUFEN_API_KEY"),
        base_url=_env("SUFEN_BASE_URL"),
        fake_provider=_env_bool("SUFEN_FAKE_PROVIDER", False),
        delegation_hmac_secret=_env("SUFEN_DELEGATION_HMAC_SECRET"),
        tavily_api_key=_env("SUFEN_TAVILY_API_KEY"),
        memory_root=Path(_env("SUFEN_MEMORY_ROOT", str(DEFAULT_MEMORY_ROOT))).expanduser(),
        bind_host=_env("SUFEN_BIND_HOST", "127.0.0.1"),
        port=port,
        home=home,
    )


def bridge_inherited_runtime_home() -> None:
    """Let inherited internals use SUFEN_HOME without exposing it publicly.

    Some retained runtime helpers still read SUFEN_HOME internally. This bridge
    is inherited runtime compatibility glue only; users configure SUFEN_HOME.
    """

    sufen_home = _env("SUFEN_HOME")
    if sufen_home and not os.environ.get("SUFEN_HOME"):
        os.environ["SUFEN_HOME"] = sufen_home
