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


MAX_TOOL_LOOP_TURNS = 4


class ProviderError(RuntimeError):
    """Raised when the production provider cannot return a SuFen response."""


def provider_tool_name(internal_name: str) -> str:
    """Return an OpenAI-compatible tool name while keeping SuFen internals stable."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", internal_name)


PROVIDER_TOOL_NAME_BY_INTERNAL = {name: provider_tool_name(name) for name in SUFEN_TOOL_NAMES}
if len(set(PROVIDER_TOOL_NAME_BY_INTERNAL.values())) != len(PROVIDER_TOOL_NAME_BY_INTERNAL):
    raise RuntimeError("SuFen provider tool name mapping is not one-to-one")
INTERNAL_TOOL_NAME_BY_PROVIDER = {safe: internal for internal, safe in PROVIDER_TOOL_NAME_BY_INTERNAL.items()}


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


def _first_present_text(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _normalize_evidence_item(item: Any, index: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {
            "source": f"provider.evidence.{index}",
            "summary": str(item),
            "confidence": 0.5,
        }
    source = _first_present_text(item, (
        "source",
        "authorizationId",
        "referenceId",
        "refId",
        "id",
        "title",
        "name",
    )) or f"provider.evidence.{index}"
    summary = _first_present_text(item, (
        "summary",
        "keyPoint",
        "keypoint",
        "point",
        "reason",
        "detail",
        "content",
        "text",
    ))
    if not summary:
        summary = json.dumps(item, ensure_ascii=False, sort_keys=True)[:800]
    try:
        confidence = float(item.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    return {
        "source": source[:200],
        "summary": summary[:1200],
        "confidence": max(0.0, min(1.0, confidence)),
    }


def _normalize_tool_audit_item(item: Any, index: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {
            "tool": f"provider.audit.{index}",
            "action": "provider_report",
            "status": str(item)[:800] or "ok",
            "draftOnly": True,
        }
    provider_name = _first_present_text(item, ("tool", "name", "function", "id")) or f"provider.audit.{index}"
    tool = INTERNAL_TOOL_NAME_BY_PROVIDER.get(provider_name, provider_name)
    action = _first_present_text(item, ("action", "operation", "op", "type")) or "provider_report"
    status = _first_present_text(item, ("status", "result", "summary", "note", "reason", "message")) or "ok"
    return {
        "tool": tool[:160],
        "action": action[:160],
        "status": status[:1200],
        "draftOnly": bool(item.get("draftOnly", True)),
    }


def _normalize_field_patch_draft(item: Any, index: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        text = str(item)
        return {
            "target": {},
            "field": f"fieldPatch.{index}",
            "before": None,
            "after": text,
            "diff": f"+{text}"[:1200],
            "reason": "provider_field_patch_draft",
            "draftOnly": True,
        }
    clean = dict(item)
    field = _first_present_text(clean, ("field", "name", "key", "path")) or f"fieldPatch.{index}"
    before = clean.get("before")
    after = clean.get("after", clean.get("value", clean.get("suggestion")))
    diff = _first_present_text(clean, ("diff", "patch", "change"))
    if not diff:
        before_text = "" if before is None else str(before)
        after_text = "" if after is None else str(after)
        diff = "\n".join([f"-{before_text}", f"+{after_text}"]).strip()
    if not diff:
        diff = json.dumps(clean, ensure_ascii=False, sort_keys=True)[:1200]
    target = clean.get("target") if isinstance(clean.get("target"), dict) else {}
    return {
        "target": target,
        "field": field[:160],
        "before": before,
        "after": after,
        "diff": diff[:2000],
        "reason": _first_present_text(clean, ("reason", "summary", "note", "message")) or "provider_field_patch_draft",
        "draftOnly": bool(clean.get("draftOnly", True)),
    }


def _normalize_event_draft(item: Any, index: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        text = str(item)
        return {
            "name": f"SuFen事件草稿{index + 1}",
            "body": text,
            "priority": "normal",
            "target": {},
            "reason": "provider_event_draft",
            "draftOnly": True,
        }
    clean = dict(item)
    return {
        "name": _first_present_text(clean, ("name", "title", "summary")) or f"SuFen事件草稿{index + 1}",
        "body": _first_present_text(clean, ("body", "content", "description", "detail", "message")) or _first_present_text(clean, ("name", "title", "summary")) or "SuFen事件草稿",
        "eventTime": clean.get("eventTime"),
        "remindTime": clean.get("remindTime"),
        "repeatType": clean.get("repeatType"),
        "priority": clean.get("priority") if clean.get("priority") in {"low", "normal", "high"} else "normal",
        "target": clean.get("target") if isinstance(clean.get("target"), dict) else {},
        "reason": _first_present_text(clean, ("reason", "summary", "note")) or "provider_event_draft",
        "draftOnly": bool(clean.get("draftOnly", True)),
    }


def _normalize_provider_response_payload(payload: dict[str, Any]) -> dict[str, Any]:
    clean = dict(payload)
    if isinstance(clean.get("evidenceUsed"), list):
        clean["evidenceUsed"] = [
            _normalize_evidence_item(item, index)
            for index, item in enumerate(clean["evidenceUsed"])
        ]
    if isinstance(clean.get("toolAudit"), list):
        clean["toolAudit"] = [
            _normalize_tool_audit_item(item, index)
            for index, item in enumerate(clean["toolAudit"])
        ]
    if isinstance(clean.get("fieldPatchDrafts"), list):
        clean["fieldPatchDrafts"] = [
            _normalize_field_patch_draft(item, index)
            for index, item in enumerate(clean["fieldPatchDrafts"])
        ]
    if isinstance(clean.get("eventDrafts"), list):
        clean["eventDrafts"] = [
            _normalize_event_draft(item, index)
            for index, item in enumerate(clean["eventDrafts"])
        ]
    if isinstance(clean.get("memoryPatch"), dict):
        memory_patch = dict(clean["memoryPatch"])
        if not isinstance(memory_patch.get("scope"), dict):
            memory_patch["scope"] = {}
        clean["memoryPatch"] = memory_patch
    return clean


def _tool_definitions() -> list[dict[str, Any]]:
    os.environ.setdefault("SUFEN_AGENT_MODE", "1")
    import tools.sufen_mystand_tools  # noqa: F401
    import tools.web_tools  # noqa: F401
    from tools.registry import registry

    definitions = registry.get_definitions(set(SUFEN_TOOL_NAMES), quiet=True)
    safe_definitions: list[dict[str, Any]] = []
    for definition in definitions:
        safe_definition = dict(definition)
        function = dict(safe_definition.get("function") or {})
        internal_name = str(function.get("name") or "").strip()
        function["name"] = PROVIDER_TOOL_NAME_BY_INTERNAL.get(internal_name, provider_tool_name(internal_name))
        safe_definition["function"] = function
        safe_definitions.append(safe_definition)
    return safe_definitions


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
        "实际 provider 工具 schema 为兼容 OpenAI/DeepSeek，工具名中的点号会映射为下划线；模型必须使用当前 schema 暴露的工具名："
        + json.dumps(PROVIDER_TOOL_NAME_BY_INTERNAL, ensure_ascii=False, sort_keys=True),
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


def build_provider_messages(prompt: str, task: SuFenTaskPackage) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": _system_message(task)},
        {"role": "user", "content": _user_message(prompt, task)},
    ]


def build_provider_payload(
    prompt: str,
    task: SuFenTaskPackage,
    settings: SuFenSettings,
    messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "model": settings.model,
        "messages": messages or build_provider_messages(prompt, task),
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


def _message_from_provider(data: dict[str, Any]) -> dict[str, Any]:
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError("provider response missing choices[0].message") from exc
    if not isinstance(message, dict):
        raise ProviderError("provider message must be an object")
    return message


def _provider_message_to_sufen(message: dict[str, Any]) -> SuFenResponse:
    content = message.get("content") or ""
    try:
        response = SuFenResponse.model_validate(_normalize_provider_response_payload(_extract_json_object(content)))
    except (ValidationError, json.JSONDecodeError) as exc:
        raise ProviderError(f"provider response failed SuFenResponse validation: {exc}") from exc
    response.toolAudit.append(
        ToolAuditItem(tool="provider.chat_completions", action="real_provider_request", status="ok")
    )
    return response


def _serialize_tool_result(result: Any) -> str:
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False, default=str)


def _tool_result_payload(result: Any) -> dict[str, Any] | None:
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _tool_call_id(tool_call: dict[str, Any], index: int) -> str:
    return str(tool_call.get("id") or f"call_{index}")


def _tool_call_name_and_args(tool_call: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    function = tool_call.get("function") or {}
    provider_name = str(function.get("name") or "").strip()
    name = INTERNAL_TOOL_NAME_BY_PROVIDER.get(provider_name, provider_name)
    raw_args = function.get("arguments") or "{}"
    if isinstance(raw_args, dict):
        args = raw_args
    else:
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"tool call {provider_name or '<missing>'} arguments were not JSON") from exc
    if not isinstance(args, dict):
        raise ProviderError(f"tool call {provider_name or '<missing>'} arguments must be an object")
    return name, args, provider_name


def _task_scope(task: SuFenTaskPackage) -> dict[str, str]:
    archive = task.archiveContext or {}
    return {
        "companyId": str(archive.get("companyId") or "company-ZYJ"),
        "operatorUserId": task.operator.userId,
        "subjectType": task.subject.type,
        "subjectId": task.subject.id,
    }


def _task_bound_tool_args(name: str, args: dict[str, Any], task: SuFenTaskPackage) -> dict[str, Any]:
    clean = dict(args)
    if name in {"mystand.archive.read", "mystand.knowledge_graph.read"}:
        clean.pop("authorizedPayload", None)
    if name in {"sufen_memory_search", "sufen_memory_patch_draft"}:
        scope = _task_scope(task)
        for key, expected in scope.items():
            if key in clean and clean.get(key) not in (None, "", expected):
                raise ProviderError(f"task scope mismatch for {key}")
            clean.pop(key, None)
        nested = clean.get("scope")
        if isinstance(nested, dict):
            for key, expected in scope.items():
                if key in nested and nested.get(key) not in (None, "", expected):
                    raise ProviderError(f"task scope mismatch for scope.{key}")
        clean.pop("scope", None)
        clean.pop("memoryRoot", None)
        clean.pop("admin", None)
    for key in (
        "authorizedPayload",
        "companyId",
        "operatorUserId",
        "subjectType",
        "subjectId",
        "archiveContext",
        "knowledgeGraphRefs",
        "scopedMemoryKey",
        "scope",
    ):
        if key in clean:
            raise ProviderError(f"model supplied task-bound field: {key}")
    return clean


def _dispatch_tool_call(
    tool_call: dict[str, Any],
    index: int,
    task: SuFenTaskPackage,
) -> tuple[dict[str, Any], ToolAuditItem]:
    name, args, provider_name = _tool_call_name_and_args(tool_call)
    if name not in SUFEN_TOOL_NAMES:
        raise ProviderError(f"unauthorized tool call: {provider_name or '<missing>'}")

    from tools.registry import registry

    result = registry.dispatch(name, _task_bound_tool_args(name, args, task), task_package=task)
    result_payload = _tool_result_payload(result)
    if result_payload and result_payload.get("ok") is False:
        reason = result_payload.get("reason") or result_payload.get("status") or "tool_failed_closed"
        raise ProviderError(f"{name} failed closed: {reason}")
    tool_message = {
        "role": "tool",
        "tool_call_id": _tool_call_id(tool_call, index),
        "name": provider_name or PROVIDER_TOOL_NAME_BY_INTERNAL.get(name, name),
        "content": _serialize_tool_result(result),
    }
    audit = ToolAuditItem(tool=name, action="provider_tool_call", status="ok")
    return tool_message, audit


def _assistant_tool_call_message(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": message.get("content"),
        "tool_calls": message.get("tool_calls") or [],
    }


def _request_provider(settings: SuFenSettings, messages: list[dict[str, Any]], prompt: str, task: SuFenTaskPackage) -> dict[str, Any]:
    payload = build_provider_payload(prompt, task, settings, messages=messages)
    headers = {
        "Authorization": f"Bearer {settings.provider_api_key}",
        "Content-Type": "application/json",
    }
    try:
        return _post_chat_completions(_chat_completions_url(settings), headers, payload)
    except httpx.HTTPError as exc:
        raise ProviderError(f"provider request failed: {exc}") from exc


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
                acceptableRefs=["SUFEN_PROVIDER_API_KEY", "SUFEN_BASE_URL", "OpenAI-compatible provider"],
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
    ensure_safe_actions(
        task,
        delegation_secret=settings.delegation_hmac_secret,
        require_delegation_token=True,
    )
    if not settings.provider_api_key.strip():
        return provider_fail_closed_response("missing_sufen_provider_api_key", "missing_provider_api_key")

    messages = build_provider_messages(prompt, task)
    loop_audit: list[ToolAuditItem] = []
    for turn in range(1, MAX_TOOL_LOOP_TURNS + 1):
        data = _request_provider(settings, messages, prompt, task)
        message = _message_from_provider(data)
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            response = _provider_message_to_sufen(message)
            response.toolAudit.extend(loop_audit)
            response.toolAudit.append(
                ToolAuditItem(tool="provider.chat_completions", action="real_provider_request", status=f"ok_turns:{turn}")
            )
            return response

        if not isinstance(tool_calls, list):
            return provider_fail_closed_response("invalid_tool_calls", "tool_calls_not_list")

        messages.append(_assistant_tool_call_message(message))
        try:
            for index, tool_call in enumerate(tool_calls):
                tool_message, audit = _dispatch_tool_call(tool_call, index, task)
                messages.append(tool_message)
                loop_audit.append(audit)
        except ProviderError as exc:
            response = provider_fail_closed_response("unauthorized_tool_call", f"rejected: {exc}")
            response.toolAudit.extend(loop_audit)
            return response

    response = provider_fail_closed_response("tool_loop_exceeded", f"max_turns:{MAX_TOOL_LOOP_TURNS}")
    response.toolAudit.extend(loop_audit)
    return response
