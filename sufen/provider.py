"""OpenAI-compatible production provider for SuFen."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx
from pydantic import ValidationError

from sufen.auth import FAIL_CLOSED_MESSAGE, extract_authorization_refs
from sufen.config import SuFenSettings, load_settings
from sufen.output import AuthorizationRequest, SuFenResponse, ToolAuditItem
from sufen.prompt.identity import build_sufen_identity_block
from sufen.task_package import SuFenTaskPackage, ensure_safe_actions
from toolsets import SUFEN_TOOL_NAMES


class ProviderError(RuntimeError):
    """Raised when the production provider cannot return a SuFen response."""


def _chat_completions_url(settings: SuFenSettings) -> str:
    base = settings.base_url.strip().rstrip("/")
    provider = settings.provider.lower().strip()
    if not base:
        if provider == "deepseek":
            return "https://api.deepseek.com/chat/completions"
        if provider == "openai":
            return "https://api.openai.com/v1/chat/completions"
        raise ProviderError("SUFEN_BASE_URL is required for this provider")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _extract_json_object(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise ProviderError("provider response did not contain JSON") from None
        payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ProviderError("provider response JSON must be an object")
    return payload


def _tool_definitions() -> list[dict[str, Any]]:
    os.environ.setdefault("SUFEN_AGENT_MODE", "1")
    import tools.sufen_mystand_tools  # noqa: F401
    import tools.web_tools  # noqa: F401
    from tools.registry import registry

    return registry.get_definitions(set(SUFEN_TOOL_NAMES), quiet=True)


def _system_message(task: SuFenTaskPackage) -> str:
    scope = {
        "companyId": task.archiveContext.get("companyId", "company-ZYJ"),
        "operatorUserId": task.operator.userId,
        "subjectType": task.subject.type,
        "subjectId": task.subject.id,
        "scene": task.scene,
        "scopedMemoryKey": task.scopedMemoryKey,
    }
    output_contract = {
        "answer": "string",
        "evidenceUsed": [],
        "missingAuthorizationRequests": [],
        "eventDrafts": [],
        "fieldPatchDrafts": [],
        "memoryPatch": None,
        "toolAudit": [],
    }
    return "\n\n".join([
        build_sufen_identity_block(),
        "你必须只返回 JSON，不要 Markdown，不要代码围栏。",
        "输出 JSON 必须符合 SuFenResponse 合同，并至少包含这些顶层字段："
        + json.dumps(output_contract, ensure_ascii=False, separators=(",", ":")),
        "本轮只允许使用 SuFen 第一版工具白名单："
        + json.dumps(SUFEN_TOOL_NAMES, ensure_ascii=False),
        "scoped memory 只能使用 My Stand taskPackage 锁定的范围，模型不得自选 memoryRoot，不得切换 admin 路径："
        + json.dumps(scope, ensure_ascii=False, sort_keys=True),
        "所有事件、字段修改、记忆修改都只能作为 draft 返回，不能直接写正式数据。",
    ])


def _user_message(prompt: str, task: SuFenTaskPackage) -> str:
    return "\n\n".join([
        "经纪人问题：",
        prompt or "",
        "My Stand 后端注入的 taskPackage：",
        json.dumps(task.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2),
    ])


def build_provider_payload(prompt: str, task: SuFenTaskPackage, settings: SuFenSettings) -> dict[str, Any]:
    return {
        "model": settings.model,
        "messages": [
            {"role": "system", "content": _system_message(task)},
            {"role": "user", "content": _user_message(prompt, task)},
        ],
        "tools": _tool_definitions(),
        "tool_choice": "auto",
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }


def _post_chat_completions(url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    with httpx.Client(timeout=60.0) as client:
        response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()


def _provider_response_to_sufen(data: dict[str, Any]) -> SuFenResponse:
    try:
        content = data["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError("provider response missing choices[0].message.content") from exc
    try:
        response = SuFenResponse.model_validate(_extract_json_object(content))
    except (ValidationError, json.JSONDecodeError) as exc:
        raise ProviderError(f"provider response failed SuFenResponse validation: {exc}") from exc
    response.toolAudit.append(
        ToolAuditItem(tool="provider.chat_completions", action="real_provider_request", status="ok")
    )
    return response


def missing_task_package_response() -> SuFenResponse:
    return SuFenResponse(
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


def provider_fail_closed_response(reason: str, status: str) -> SuFenResponse:
    return SuFenResponse(
        answer=FAIL_CLOSED_MESSAGE,
        missingAuthorizationRequests=[
            AuthorizationRequest(
                reason=reason,
                acceptableRefs=["SUFEN_API_KEY", "SUFEN_BASE_URL", "OpenAI-compatible provider"],
                message=FAIL_CLOSED_MESSAGE,
            )
        ],
        toolAudit=[ToolAuditItem(tool="provider.chat_completions", action="real_provider_request", status=status)],
    )


def answer_with_provider(
    prompt: str,
    *,
    task: SuFenTaskPackage | None,
    settings: SuFenSettings | None = None,
) -> SuFenResponse:
    settings = settings or load_settings()
    if task is None:
        return missing_task_package_response()
    ensure_safe_actions(task, delegation_secret=settings.delegation_hmac_secret)
    if not settings.api_key.strip():
        return provider_fail_closed_response("missing_sufen_api_key", "missing_api_key")

    payload = build_provider_payload(prompt, task, settings)
    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json",
    }
    try:
        data = _post_chat_completions(_chat_completions_url(settings), headers, payload)
    except httpx.HTTPError as exc:
        raise ProviderError(f"provider request failed: {exc}") from exc
    return _provider_response_to_sufen(data)
