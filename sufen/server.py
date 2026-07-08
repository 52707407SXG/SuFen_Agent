"""SuFen HTTP API."""

from __future__ import annotations

import os
import secrets
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel

from sufen import __version__
from sufen.auth import FAIL_CLOSED_MESSAGE
from sufen.chat import answer_sufen
from sufen.config import load_settings
from sufen.output import AuthorizationRequest, DialogueDigest, DialogueSubjectRelevance, SuFenResponse, ToolAuditItem
from sufen.provider import ProviderError
from sufen.task_package import SuFenTaskPackage


class ChatRequest(BaseModel):
    query: str
    taskPackage: SuFenTaskPackage | None = None


def _request_api_key(request: Request) -> str:
    auth = (request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("x-sufen-api-key") or "").strip()


def _require_chat_auth(request: Request, configured_key: str) -> None:
    if not configured_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "sufen_service_api_key_not_configured"},
        )
    supplied = _request_api_key(request)
    if not supplied:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "missing_sufen_service_api_key"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not secrets.compare_digest(supplied, configured_key):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "invalid_sufen_service_api_key"},
        )


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
    async def chat(request: ChatRequest, raw_request: Request) -> dict[str, Any]:
        _require_chat_auth(raw_request, settings.service_api_key)
        if request.taskPackage is None:
            response = SuFenResponse(
                answer=FAIL_CLOSED_MESSAGE,
                dialogueDigest=DialogueDigest(
                    coreIntent="缺少 My Stand taskPackage，无法进入受控档案分析",
                    discussionSummary="SuFen 没有收到后端授权的当前任务包，因此不能读取档案或做业务判断。",
                    finalOutcome="本轮未形成业务结论，需要 My Stand 后端重新发起带 taskPackage 的请求。",
                    userAcceptance="unclear",
                    subjectRelevance=DialogueSubjectRelevance(level="none", shouldPersist=False, reason="缺少 taskPackage，不属于任何当前档案。"),
                ),
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
            response = answer_sufen(request.query, task=request.taskPackage, settings=settings)
        except (ProviderError, ValueError) as exc:
            response = SuFenResponse(
                answer=FAIL_CLOSED_MESSAGE,
                dialogueDigest=DialogueDigest(
                    coreIntent="SuFen 未能完成本轮受控回答",
                    discussionSummary=f"SuFen 进入 fail-closed 分支：{exc}",
                    finalOutcome="本轮未形成可入档的业务结论，需要恢复 provider/tool 输出后再判断。",
                    userAcceptance="unclear",
                    subjectRelevance=DialogueSubjectRelevance(level="none", shouldPersist=False, reason="server_fail_closed"),
                ),
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
