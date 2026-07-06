"""SuFen HTTP API."""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from sufen import __version__
from sufen.auth import FAIL_CLOSED_MESSAGE
from sufen.config import load_settings
from sufen.fake_provider import answer_with_fake_provider
from sufen.output import AuthorizationRequest, SuFenResponse, ToolAuditItem
from sufen.task_package import SuFenTaskPackage


class ChatRequest(BaseModel):
    query: str
    taskPackage: SuFenTaskPackage | None = None


def create_app() -> FastAPI:
    os.environ.setdefault("SUFEN_AGENT_MODE", "1")
    settings = load_settings()
    app = FastAPI(title="SuFen-Agent", version=__version__)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "sufen-agent",
            "version": __version__,
            "provider": settings.provider,
            "model": settings.model,
        }

    @app.post("/v1/chat")
    async def chat(request: ChatRequest) -> dict[str, Any]:
        if request.taskPackage is None:
            response = SuFenResponse(
                answer=FAIL_CLOSED_MESSAGE,
                missingAuthorizationRequests=[
                    AuthorizationRequest(
                        reason="missing_task_package",
                        acceptableRefs=["My Stand taskPackage"],
                        message=FAIL_CLOSED_MESSAGE,
                    )
                ],
                toolAudit=[
                    ToolAuditItem(tool="task_package", action="require_backend_injected_scope", status="missing")
                ],
            )
            return response.model_dump(mode="json")
        try:
            response = answer_with_fake_provider(request.query, task=request.taskPackage)
        except ValueError as exc:
            response = SuFenResponse(
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
        return response.model_dump(mode="json")

    return app
