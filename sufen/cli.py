"""SuFen command-line entry point."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from sufen import __version__
from sufen.auth import FAIL_CLOSED_MESSAGE
from sufen.config import bridge_inherited_runtime_home, load_settings
from sufen.fake_provider import answer_with_fake_provider
from sufen.output import AuthorizationRequest, SuFenResponse, ToolAuditItem
from sufen.task_package import SuFenTaskPackage


def _load_task_package(path: str | None) -> SuFenTaskPackage | None:
    if not path:
        return None
    with Path(path).open("r", encoding="utf-8") as handle:
        return SuFenTaskPackage.model_validate(json.load(handle))


def _cmd_chat(args: argparse.Namespace) -> int:
    task = _load_task_package(args.task_package)
    try:
        result = answer_with_fake_provider(args.query or "", task=task)
    except ValueError as exc:
        result = SuFenResponse(
            answer=FAIL_CLOSED_MESSAGE,
            missingAuthorizationRequests=[
                AuthorizationRequest(
                    reason="unsafe_task_package",
                    acceptableRefs=["My Stand backend-injected taskPackage"],
                    message=FAIL_CLOSED_MESSAGE,
                )
            ],
            toolAudit=[
                ToolAuditItem(tool="task_package", action="validate_scope", status=f"rejected: {exc}")
            ],
        )
    print(result.model_dump_json(indent=2, ensure_ascii=False))
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = load_settings()
    host = args.host or settings.bind_host
    port = args.port or settings.port
    uvicorn.run("sufen.server:create_app", factory=True, host=host, port=port)
    return 0


def _cmd_doctor(_: argparse.Namespace) -> int:
    settings = load_settings()
    print("SuFen-Agent Doctor")
    print(f"home: {settings.home}")
    print(f"memory_root: {settings.memory_root}")
    print(f"provider: {settings.provider}")
    print(f"model: {settings.model}")
    print(f"api_key: {'set' if settings.api_key else 'missing'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sufen", description="SuFen-Agent for My Stand archive strategy.")
    parser.add_argument("--version", action="store_true", help="Show SuFen-Agent version and exit")
    subparsers = parser.add_subparsers(dest="command")

    chat = subparsers.add_parser("chat", help="Run a local SuFen dry-run conversation")
    chat.add_argument("-q", "--query", default="", help="User question or My Stand prompt")
    chat.add_argument("--task-package", help="Path to a My Stand task package JSON file")
    chat.set_defaults(func=_cmd_chat)

    serve = subparsers.add_parser("serve", help="Start SuFen HTTP API")
    serve.add_argument("--host", help="Bind host, defaults to SUFEN_BIND_HOST")
    serve.add_argument("--port", type=int, help="Bind port, defaults to SUFEN_PORT")
    serve.set_defaults(func=_cmd_serve)

    doctor = subparsers.add_parser("doctor", help="Check SuFen local configuration")
    doctor.set_defaults(func=_cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("SUFEN_AGENT_MODE", "1")
    os.environ.setdefault("SUFEN_COMMAND_NAME", "sufen")
    bridge_inherited_runtime_home()

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version or args.command == "version":
        print(f"SuFen-Agent v{__version__}")
        return 0
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
