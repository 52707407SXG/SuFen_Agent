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
from sufen.output import AuthorizationRequest, DialogueDigest, DialogueSubjectRelevance, EvidenceItem, SuFenResponse, ToolAuditItem
from sufen.prompt.identity import build_sufen_identity_block
from sufen.task_package import SuFenTaskPackage, ensure_safe_actions
from sufen.time import now as sufen_now
from toolsets import SUFEN_TOOL_NAMES


MAX_TOOL_LOOP_TURNS = 4
AUTHORIZED_CONTEXT_RETRY_MARKER = "后端已授权当前资料事实卡"
BUSINESS_INTENT_RE = re.compile(
    r"房源|房子|业主|客户|客源|售后|经纪人|业绩|成交|签单|价格|报价|底价|降价|维护|带看|挂牌|谈判|沟通|"
    r"话术|策略|判断|分析|怎么看|怎么样|怎么聊|怎么谈|怎么办|下一步|资料|档案|图谱|结算|合同|回款|"
    r"评分|等级|概率|行情|同户型|装修|税费"
)
GREETING_RE = re.compile(r"^(?:sufen|su\s*fen|素分|素芬|苏芬|苏分)?(?:，|,|\s)*(你好|晚上好|早上好|中午好|下午好|晚安|在吗|还在吗|睡了吗)[。！？!?，,\s]*$", re.I)
ACK_RE = re.compile(r"^(?:sufen|su\s*fen|素分|素芬|苏芬|苏分)?(?:，|,|\s)*(收到|好的|好|嗯|行|谢谢|辛苦了)[。！？!?，,\s]*$", re.I)


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


def _archive_context(task: SuFenTaskPackage) -> dict[str, Any]:
    return task.archiveContext if isinstance(task.archiveContext, dict) else {}


def _raw_user_message(prompt: str, task: SuFenTaskPackage) -> str:
    archive_context = _archive_context(task)
    for key in ("userMessageForSufen", "userMessage", "rawUserMessage", "latestUserMessage"):
        value = archive_context.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    text = str(prompt or "").strip()
    for pattern in (
        r"用户本轮原话(?:（最高优先用于意图判断）)?：\s*(.*?)(?:\n\n|$)",
        r"经纪人问题：\s*(.*?)(?:\n\nMy Stand 后端注入|$)",
    ):
        match = re.search(pattern, text, flags=re.S)
        if match and match.group(1).strip():
            return match.group(1).strip()
    return text


def _classify_user_intent(prompt: str, task: SuFenTaskPackage) -> str:
    archive_context = _archive_context(task)
    raw_backend_intent = str(archive_context.get("sufenUserIntent") or archive_context.get("userIntent") or "").strip()
    if raw_backend_intent in {
        "casual_greeting",
        "casual_ack",
        "fact_query",
        "strategy_question",
        "owner_communication",
        "report_request",
        "business_question",
    }:
        return raw_backend_intent
    raw = _raw_user_message(prompt, task)
    compact = re.sub(r"\s+", "", raw)
    if not compact:
        return "unknown"
    has_business = bool(BUSINESS_INTENT_RE.search(compact))
    if GREETING_RE.match(compact) and not has_business:
        return "casual_greeting"
    if ACK_RE.match(compact) and not has_business:
        return "casual_ack"
    if re.search(r"业主.*(怎么|沟通|开口|聊|话术)|这个业主|这种业主", compact):
        return "owner_communication"
    if re.search(r"报告|复盘|完整分析|全面分析|展开说|详细", compact):
        return "report_request"
    if re.search(r"策略|谋略|判断|怎么看|怎么样|怎么办|下一步|怎么谈|怎么聊|抓手", compact):
        return "strategy_question"
    if has_business:
        return "business_question"
    return "casual_chat"


def _operator_salutation(task: SuFenTaskPackage) -> str:
    operator = task.operator
    if bool(getattr(operator, "isGangGe", False)) or str(getattr(operator, "userId", "")).strip() == "52707407":
        return "刚哥"
    name = str(getattr(operator, "name", "") or "").strip()
    return name or "我"


def _short_casual_answer(raw_user_message: str, task: SuFenTaskPackage) -> str:
    salutation = _operator_salutation(task)
    compact = re.sub(r"\s+", "", raw_user_message or "")
    if "晚安" in compact:
        return f"{salutation}，晚安。我在。"
    if "晚上好" in compact:
        return f"{salutation}晚上好，我在。你先说，我听着。"
    if "早上好" in compact:
        return f"{salutation}早上好，我在。"
    if "中午好" in compact:
        return f"{salutation}中午好，我在。"
    if "下午好" in compact:
        return f"{salutation}下午好，我在。"
    if "在吗" in compact or "还在吗" in compact:
        return f"{salutation}，我在。"
    if re.search(r"谢谢|辛苦了", compact):
        return f"{salutation}，收到。"
    return f"{salutation}，我在。"


def _extract_json_object(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        payload = json.loads(text, strict=False)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise ProviderError("provider response did not contain JSON") from None
        payload = json.loads(match.group(0), strict=False)
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


def _normalize_authorization_request(item: Any, index: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        text = str(item).strip()
        return {
            "reason": f"provider.missing_authorization.{index}",
            "acceptableRefs": [],
            "message": text or FAIL_CLOSED_MESSAGE,
        }
    reason = _first_present_text(item, (
        "reason",
        "type",
        "kind",
        "source",
        "authorizationId",
        "referenceId",
        "refId",
        "id",
        "title",
        "name",
    )) or f"provider.missing_authorization.{index}"
    message = _first_present_text(item, (
        "message",
        "description",
        "summary",
        "detail",
        "content",
        "text",
        "note",
    )) or reason
    refs = item.get("acceptableRefs")
    if not isinstance(refs, list):
        refs = item.get("refs") if isinstance(item.get("refs"), list) else []
    single_ref = _first_present_text(item, ("authorizationId", "referenceId", "refId", "id", "ref"))
    acceptable_refs = [str(ref).strip() for ref in refs if str(ref).strip()]
    if single_ref and single_ref not in acceptable_refs:
        acceptable_refs.append(single_ref)
    return {
        "reason": reason[:200],
        "acceptableRefs": acceptable_refs[:12],
        "message": message[:1200],
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


def _normalize_memory_list(value: Any) -> list[str]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _normalize_dialogue_digest(value: Any) -> dict[str, Any] | None:
    if value in (None, "", [], {}):
        return None
    if not isinstance(value, dict):
        text = str(value).strip()
        return {
            "coreIntent": text[:160],
            "discussionSummary": "",
            "finalOutcome": "",
            "userAcceptance": "unclear",
            "subjectRelevance": {"level": "none", "shouldPersist": False, "reason": "dialogueDigest was not an object"},
        }
    clean = dict(value)
    acceptance = str(clean.get("userAcceptance") or "").strip().lower()
    acceptance_map = {
        "accept": "accepted",
        "accepted": "accepted",
        "yes": "accepted",
        "adopted": "accepted",
        "采纳": "accepted",
        "已采纳": "accepted",
        "reject": "rejected",
        "rejected": "rejected",
        "no": "rejected",
        "未采纳": "rejected",
        "不同意": "rejected",
        "chat": "chat",
        "casual": "chat",
        "闲聊": "chat",
        "寒暄": "chat",
        "unclear": "unclear",
    }
    relevance = clean.get("subjectRelevance") if isinstance(clean.get("subjectRelevance"), dict) else {}
    level = str(relevance.get("level") or "").strip().lower()
    if level not in {"direct", "indirect", "none"}:
        level = "none"
    return {
        "coreIntent": str(clean.get("coreIntent") or "").strip()[:220],
        "discussionSummary": str(clean.get("discussionSummary") or "").strip()[:620],
        "finalOutcome": str(clean.get("finalOutcome") or "").strip()[:320],
        "userAcceptance": acceptance_map.get(acceptance, "unclear"),
        "subjectRelevance": {
            "level": level,
            "shouldPersist": bool(relevance.get("shouldPersist") is True),
            "reason": str(relevance.get("reason") or "").strip()[:320],
        },
    }


def _normalize_provider_response_payload(payload: dict[str, Any]) -> dict[str, Any]:
    clean = dict(payload)
    clean["dialogueDigest"] = _normalize_dialogue_digest(clean.get("dialogueDigest"))
    if isinstance(clean.get("evidenceUsed"), list):
        clean["evidenceUsed"] = [
            _normalize_evidence_item(item, index)
            for index, item in enumerate(clean["evidenceUsed"])
        ]
    if isinstance(clean.get("missingAuthorizationRequests"), list):
        clean["missingAuthorizationRequests"] = [
            _normalize_authorization_request(item, index)
            for index, item in enumerate(clean["missingAuthorizationRequests"])
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
    clean["memoryPatch"] = None
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


def _short_text(value: Any, limit: int = 6000) -> Any:
    if isinstance(value, str):
        text = value.strip()
        return text if len(text) <= limit else text[:limit] + "..."
    return value


def _compact_authorized_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return _short_text(json.dumps(value, ensure_ascii=False, sort_keys=True), 1200)
    if isinstance(value, dict):
        return {
            str(key): _compact_authorized_value(item, depth=depth + 1)
            for key, item in value.items()
            if item not in (None, "", [], {})
        }
    if isinstance(value, list):
        return [_compact_authorized_value(item, depth=depth + 1) for item in value[:30]]
    return _short_text(value, 3000)


def _authorized_context_payload(task: SuFenTaskPackage) -> dict[str, Any]:
    archive_context = task.archiveContext or {}
    payload: dict[str, Any] = {}
    for key in (
        "actualArchiveId",
        "authorizationId",
        "archive",
        "broker",
        "archiveSummary",
        "archiveRows",
        "parserToolResults",
        "parserToolSummary",
        "referenceContext",
    ):
        value = archive_context.get(key)
        if value not in (None, "", [], {}):
            payload[key] = _compact_authorized_value(value)
    return payload


def _has_backend_authorized_context(task: SuFenTaskPackage) -> bool:
    payload = _authorized_context_payload(task)
    return bool(
        payload.get("archive")
        or payload.get("broker")
        or payload.get("archiveSummary")
        or payload.get("archiveRows")
    )


def _authorized_context_card(task: SuFenTaskPackage) -> str:
    payload = _authorized_context_payload(task)
    if not payload:
        return (
            f"{AUTHORIZED_CONTEXT_RETRY_MARKER}：本轮 taskPackage 没有注入当前档案正文；"
            "若用户问题需要档案事实，必须按缺资料处理。"
        )
    return (
        f"{AUTHORIZED_CONTEXT_RETRY_MARKER}：以下内容由 My Stand 后端按当前登录账号权限注入，"
        "就是本轮当前档案/经纪人档案的可读资料。用户问“当前档案”“这个客户/业主/售后/经纪人”时，"
        "必须优先读取这里的资料并回答，不得要求用户再提供站内ID；但必须严格遵守 sourceQuality、"
        "verifiedFacts、legacySignals 和 dialogueLogBrief 的证据分级，usableAsCurrentFact=false 的内容不能说成当前事实。\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)[:30_000]
    )


def _runtime_anchor_card(task: SuFenTaskPackage) -> str:
    current_time = sufen_now()
    archive_context = task.archiveContext or {}
    operator = task.operator.model_dump(mode="json")
    subject = task.subject.model_dump(mode="json")
    anchor = {
        "currentSufenTime": current_time.isoformat(),
        "timezone": str(current_time.tzinfo or "Asia/Shanghai"),
        "scene": task.scene,
        "operator": operator,
        "subject": subject,
        "sufenMode": archive_context.get("sufenMode") or archive_context.get("reasoningMode") or "normal",
        "strategyModeDirective": archive_context.get("strategyModeDirective"),
        "subjectRelationHint": archive_context.get("subjectRelationHint"),
        "module": archive_context.get("module") or archive_context.get("moduleName"),
        "dialogueLogKey": task.dialogueLogKey,
        "requiredKnowledgeGraph": task.requiredKnowledgeGraph or archive_context.get("requiredKnowledgeGraph"),
        "knowledgeGraphBinding": archive_context.get("knowledgeGraphBinding"),
        "contextLoadPlanVersion": (archive_context.get("contextLoadPlan") or {}).get("version")
        if isinstance(archive_context.get("contextLoadPlan"), dict)
        else None,
    }
    strategy_mode = str(anchor["sufenMode"] or "").strip().lower() == "strategy"
    strategy_text = (
        "用户已显式开启谋略模式：本轮必须更重视人情关系、真实意图、时机、风险、后手和长期影响；"
        "先听后说，资料不足时只问一个最关键问题；资料足够时给有取舍的判断、话术和下一步。"
        "谋略模式不是话痨模式，不得为了显得深而堆长篇，不得暴露推理链，不得越权加载未授权资料。"
        if strategy_mode
        else ""
    )
    return (
        "本轮 SuFen 执行锚点：回答前先识别操作者、当前模块、当前档案对象、操作者与档案对象关系、"
        "北京时间和真实意图；目标不清时轻轻确认，目标清楚时直接判断；按需读取最小充分资料，"
        "不得默认全量扫描、不得默认读取结算卡/财务明细/点没点结算、不得把未 loaded 资料当成已读；"
        "SuFen 只能只读检索单一人工 memory 根目录，不能写 memory，不能输出 memoryPatch；"
        "历史对话只能按 dialogueLogKey 和 taskPackage.archiveContext.dialogueLogBrief 做摘要续接；"
        "个人业务档案默认先做管理判断，不做财务表播报；未触发财务明细层不得引用合同号、确认、结算或凭证字段；"
        "manager_confirmed/店长确认不是经纪人本人确认；"
        "必须检查 requiredKnowledgeGraph/knowledgeGraphBinding，不能把房源维护、客户跟进、售后维护、经纪人成长路径四类图谱混用；"
        "闲聊可以自然聊，但要有边界，不做话痨，也不压迫用户进入业务。"
        + (f"\n{strategy_text}" if strategy_text else "")
        + "\n"
        + json.dumps(anchor, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _system_message(task: SuFenTaskPackage) -> str:
    scope = {
        "companyId": task.archiveContext.get("companyId", "company-ZYJ"),
        "operatorUserId": task.operator.userId,
        "subjectType": task.subject.type,
        "subjectId": task.subject.id,
        "scene": task.scene,
        "dialogueLogKey": task.dialogueLogKey,
        "requiredKnowledgeGraph": task.requiredKnowledgeGraph or task.archiveContext.get("requiredKnowledgeGraph"),
    }
    output_contract = {
        "answer": "string",
        "dialogueDigest": {
            "coreIntent": "一句话写清用户真正想解决什么",
            "discussionSummary": "两三句话压缩本轮讨论过程：SuFen 建议、用户是否反对、如何调整",
            "finalOutcome": "一句话写最后采纳、暂定或未形成结论的结果",
            "userAcceptance": "accepted|rejected|unclear|chat",
            "subjectRelevance": {
                "level": "direct|indirect|none",
                "shouldPersist": False,
                "reason": "为什么这条摘要跟当前档案有关；无关时说明不应沉淀",
            },
        },
        "evidenceUsed": [],
        "missingAuthorizationRequests": [],
        "eventDrafts": [],
        "fieldPatchDrafts": [],
        "memoryPatch": None,
        "toolAudit": [],
    }
    return "\n\n".join([
        build_sufen_identity_block(),
        "你必须只返回 JSON，不要代码围栏；当回答较长、需要讲解、复盘、拆步骤、列依据、给话术或做表格对比时，answer 字段可以并应优先使用 Markdown。闲聊或短答可以是普通文本。",
        "输出 JSON 必须符合 SuFenResponse 合同，并至少包含这些顶层字段："
        + json.dumps(output_contract, ensure_ascii=False, separators=(",", ":")),
        "本轮只允许使用 SuFen 第一版工具白名单："
        + json.dumps(SUFEN_TOOL_NAMES, ensure_ascii=False),
        "实际 provider 工具 schema 为兼容 OpenAI/DeepSeek，工具名中的点号会映射为下划线；模型必须使用当前 schema 暴露的工具名："
        + json.dumps(PROVIDER_TOOL_NAME_BY_INTERNAL, ensure_ascii=False, sort_keys=True),
        "SuFen 的长期 memory 是单一人工维护根目录，只能通过 sufen_memory_search 只读检索；模型不得自选 memoryRoot，不得创建 scoped memory，不得输出 memoryPatch："
        + json.dumps(scope, ensure_ascii=False, sort_keys=True),
        _runtime_anchor_card(task),
        "用户本轮原话是最高优先级的意图来源；taskPackage、档案事实包、图谱状态和历史日志只是后台参考材料。先判断用户是在寒暄、确认、问事实、问策略还是要报告，再决定要不要展开资料。寒暄和简单确认不得因为当前页面有档案或图谱缺失而输出档案名、业主名、低置信度、资料缺口或方法论缺口。",
        "My Stand taskPackage.archiveContext.archive、archiveContext.broker、archiveContext.archiveRows、archiveSummary、parserToolResults、referenceContext 和 systemFoundationContext 是后端已按权限注入的当前可读资料；只要这些字段里已有当前档案资料，必须直接读取并据此回答，不得因为用户没有额外粘贴 AUTH/OUT/KGREF 就说当前档案缺资料。但 archive 内部必须按 verifiedFacts/sourceQuality/legacySignals 分级使用，usableAsCurrentFact=false 的内容只能做低置信度线索，不能当当前事实。",
        "必须遵守 taskPackage.archiveContext.contextLoadPlan：先用 loaded 层轻量回应或确认意图，目标明确后再按触发条件展开特征卡、房源笔记、图片/OCR、知识图谱等未加载层；未标记 loaded 的资料不得假装已读。",
        "必须遵守 requiredKnowledgeGraph/knowledgeGraphBinding：经纪人个人业务档案只用“经纪人成长路径”，房源维护只用“房源维护”，客户跟进只用“客户跟进”，售后维护只用“售后维护”。只有当用户明确要业务判断、策略路径、方法论或报告时，图谱缺失、未授权或为空才需要用自然语言点出资料边界；寒暄、目标确认和简单事实回答不得被图谱缺口阻塞，也不得输出“低置信度”口头禅。",
        "每轮必须填写 dialogueDigest，专供 My Stand 后端判断是否写入查看日志。dialogueDigest 要极致压缩但准确：coreIntent 一句话，discussionSummary 两三句话，finalOutcome 一句话；subjectRelevance.shouldPersist 必须保守，只有内容确实服务当前入口和当前档案对象时才为 true。寒暄、闲聊、测试能力、跑题、别的档案内容、原始附件全文、临时财务明细都不得建议沉淀。",
        _authorized_context_card(task),
        "所有事件和字段修改都只能作为 draft 返回，不能直接写正式数据；SuFen 不返回记忆修改草稿。",
    ])


def _user_message(prompt: str, task: SuFenTaskPackage) -> str:
    return "\n\n".join([
        "用户本轮原话（最高优先用于意图判断）：",
        _raw_user_message(prompt, task),
        "经纪人问题和本轮额外材料：",
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


def _looks_like_wrong_missing_context_response(response: SuFenResponse, task: SuFenTaskPackage) -> bool:
    if not _has_backend_authorized_context(task):
        return False
    answer = response.answer or ""
    if FAIL_CLOSED_MESSAGE in answer:
        return True
    missing_reasons = " ".join(item.reason for item in response.missingAuthorizationRequests)
    if missing_reasons and re.search(r"missing|authorization|required|archive|reference", missing_reasons, flags=re.I):
        return True
    return bool(re.search(r"缺关键资料|站内ID|提供.*ID|没有.*资料", answer))


def _authorized_context_retry_prompt(prompt: str, response: SuFenResponse, task: SuFenTaskPackage) -> str:
    return "\n\n".join([
        "上一轮输出错误地要求用户补站内ID，但本轮 taskPackage 已经包含后端授权的当前资料。",
        "请重新回答：必须直接读取“后端已授权当前资料事实卡”和 taskPackage.archiveContext 中的事实，复述用户要求的关键资料，再给建议。",
        "禁止继续要求用户提供当前档案站内ID；只有事实卡和 taskPackage 都没有目标资料时才允许说缺资料。",
        "上一轮错误回答：",
        response.answer,
        "原始经纪人问题：",
        prompt or "",
        _authorized_context_card(task),
    ])


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "；".join(_text(item) for item in value if _text(item))
    if isinstance(value, dict):
        return "；".join(f"{key}: {_text(item)}" for key, item in value.items() if _text(item))
    return str(value).strip()


def _archive_title(archive: dict[str, Any], task: SuFenTaskPackage) -> str:
    return (
        _text(archive.get("displayName"))
        or _text(archive.get("name"))
        or _text(archive.get("ownerName"))
        or task.subject.id
    )


def _fallback_field_rows(archive: dict[str, Any]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    title = _text(archive.get("displayName")) or _text(archive.get("name")) or _text(archive.get("id"))
    if title:
        rows.append(("档案标题", title))
    verified = archive.get("verifiedFacts") if isinstance(archive.get("verifiedFacts"), dict) else {}
    fields = verified.get("fields") if isinstance(verified.get("fields"), dict) else archive.get("fields")
    if isinstance(fields, dict):
        for key, value in fields.items():
            text = _text(value)
            if text:
                rows.append((str(key), text))
    events = verified.get("events") if isinstance(verified.get("events"), list) else archive.get("events")
    if isinstance(events, list) and events:
        rows.append(("当前事件", f"{len(events)} 条已授权事件，仅按事件原文做事实来源"))
    source_quality = archive.get("sourceQuality") if isinstance(archive.get("sourceQuality"), dict) else {}
    score_quality = source_quality.get("score") if isinstance(source_quality.get("score"), dict) else {}
    if score_quality:
        status = _text(score_quality.get("status")) or "unknown"
        usable = bool(score_quality.get("usableAsCurrentFact"))
        rows.append(("评分证据", "已校验结构化评分" if usable else f"评分未校验（{status}），不能当正式分数"))
    completeness = source_quality.get("evidenceCompleteness") if isinstance(source_quality.get("evidenceCompleteness"), dict) else {}
    if completeness:
        rows.append(("资料厚度", _text(completeness.get("reason"))[:240]))
    return rows[:24]


def _authorized_context_fallback_response(
    *,
    prompt: str,
    previous_response: SuFenResponse,
    task: SuFenTaskPackage,
    loop_audit: list[ToolAuditItem],
) -> SuFenResponse:
    archive_context = task.archiveContext or {}
    archive = archive_context.get("archive") if isinstance(archive_context.get("archive"), dict) else {}
    broker = archive_context.get("broker") if isinstance(archive_context.get("broker"), dict) else {}
    subject_payload = archive or broker
    title = _archive_title(subject_payload, task) if subject_payload else task.subject.id
    rows = _fallback_field_rows(subject_payload) if subject_payload else []
    rows_markdown = "\n".join(f"| {key} | {value} |" for key, value in rows) or "| 当前资料 | taskPackage 已授权，但可展示字段为空 |"
    facts_text = "；".join(f"{key}: {value}" for key, value in rows[:12])
    scene = task.scene or "当前档案"
    answer = (
        f"**当前{scene}档案：{title}**\n\n"
        "| 字段 | 内容 |\n|---|---|\n"
        f"{rows_markdown}\n\n"
        "**判断**\n"
        "- 以上只列 My Stand 本轮已授权的可展示事实和证据状态；旧状态、旧摘要、旧分数、称呼和历史日志不在这里当事实使用。\n"
        "- 如果你问的是“这套房子怎么样”，这份兜底回答只能低置信度收口，不能替代房源笔记、公开行情、带看反馈和业主沟通证据。\n\n"
        "**下一步**\n"
        "1. 先补一个关键证据：房源笔记、同小区同户型行情、最近带看反馈或业主真实底线。\n"
        "2. 证据补齐后再判断等级、维护策略或话术；现在不要硬下结论。\n"
        "3. 如果要生成事件、字段修改或外发话术，继续走草稿确认，不直接写正式数据。"
    )
    return SuFenResponse(
        answer=answer,
        dialogueDigest=DialogueDigest(
            coreIntent=f"读取当前{scene}档案并围绕本轮问题给出初步判断",
            discussionSummary=f"模型误判缺资料后，SuFen 使用 My Stand 后端已授权注入的 taskPackage 读取当前档案 {title}，并提示后续按当前档案事实确认沟通目标。",
            finalOutcome="本轮只给出基于当前授权事实卡的低置信度兜底说明，未形成可入档的正式业务判断。",
            userAcceptance="unclear",
            subjectRelevance=DialogueSubjectRelevance(
                level="direct" if subject_payload else "none",
                shouldPersist=False,
                reason="兜底回答只用于纠正模型缺资料误判，低置信度且未形成正式业务结论，不建议沉淀。" if subject_payload else "缺少当前档案对象，不建议沉淀。",
            ),
        ),
        evidenceUsed=[
            EvidenceItem(
                source="taskPackage.archiveContext",
                summary=f"模型误判缺资料后，SuFen 按后端授权 taskPackage 读取当前资料：{facts_text[:1000] or title}",
                confidence=0.58,
            )
        ],
        missingAuthorizationRequests=[],
        memoryPatch=None,
        toolAudit=[
            *loop_audit,
            *previous_response.toolAudit,
            ToolAuditItem(
                tool="provider.chat_completions",
                action="real_provider_request",
                status="ok_context_fallback",
            ),
            ToolAuditItem(
                tool="provider.chat_completions",
                action="authorized_context_fallback",
                status="model_still_requested_missing_data_after_retry",
            ),
        ],
    )


def _compact_sparse_property_answer_if_needed(
    response: SuFenResponse,
    *,
    task: SuFenTaskPackage,
    prompt: str,
) -> SuFenResponse:
    if task.subject.type != "property":
        return response
    if not re.search(r"这套房子怎么样|这套房.*怎么样|能不能算好房|值不值得|怎么判断|房子到底", prompt or ""):
        return response
    archive = task.archiveContext.get("archive") if isinstance(task.archiveContext, dict) else {}
    if not isinstance(archive, dict):
        return response
    source_quality = archive.get("sourceQuality") if isinstance(archive.get("sourceQuality"), dict) else {}
    completeness = source_quality.get("evidenceCompleteness") if isinstance(source_quality.get("evidenceCompleteness"), dict) else {}
    if completeness.get("status") != "current_facts_sparse":
        return response
    answer = response.answer or ""
    numbered_items = len(re.findall(r"(?:^|\n)\s*(?:[-*]|\d+[.、])\s+", answer))
    if len(answer) <= 620 and numbered_items <= 4:
        return response

    verified = archive.get("verifiedFacts") if isinstance(archive.get("verifiedFacts"), dict) else {}
    fields = verified.get("fields") if isinstance(verified.get("fields"), dict) else archive.get("fields")
    facts: list[str] = []
    if isinstance(fields, dict):
        for key in ("楼盘", "房号", "面积", "户型", "报价", "装修"):
            value = _text(fields.get(key))
            if value:
                facts.append(f"{key}{value}")
            if len(facts) >= 4:
                break
    facts_text = "，".join(facts) or "当前只看到很少的基础字段"
    response.answer = (
        f"先别急着定等级。现在只够看这些基础事实：{facts_text}。"
        "房源笔记、同户型行情、带看反馈和业主底线还没核实，我不能给分数、成交概率或完整结论。"
        "下一步先补一个关键证据：把房源笔记和同户型行情核出来，再判断这套房子值不值得重点维护。"
    )
    if response.dialogueDigest is not None:
        response.dialogueDigest.discussionSummary = (
            "当前房源事实稀疏，SuFen 收住回答，只保留已知字段、关键缺口和一个补数动作。"
        )
        response.dialogueDigest.finalOutcome = "本轮未形成房源等级、评分或成交概率判断，下一步先补房源笔记和同户型行情。"
        response.dialogueDigest.subjectRelevance.shouldPersist = False
        response.dialogueDigest.subjectRelevance.reason = "资料稀疏的克制回答，不作为正式维护结论沉淀。"
    response.toolAudit.append(
        ToolAuditItem(
            tool="provider.postprocess",
            action="compact_sparse_property_answer",
            status="ok",
        )
    )
    return response


def _prefix_property_owner_boundary_if_needed(
    response: SuFenResponse,
    *,
    task: SuFenTaskPackage,
    prompt: str,
) -> SuFenResponse:
    if task.subject.type != "property":
        return response
    if not re.search(r"业主.*(怎么|沟通|开口|聊|话术)|这个业主|这种业主", prompt or ""):
        return response
    archive = task.archiveContext.get("archive") if isinstance(task.archiveContext, dict) else {}
    if not isinstance(archive, dict):
        return response
    source_quality = archive.get("sourceQuality") if isinstance(archive.get("sourceQuality"), dict) else {}
    subject_card = source_quality.get("subjectFeatureCard") if isinstance(source_quality.get("subjectFeatureCard"), dict) else {}
    completeness = source_quality.get("evidenceCompleteness") if isinstance(source_quality.get("evidenceCompleteness"), dict) else {}
    if subject_card.get("status") not in {"not_loaded", "configured_empty", "missing", ""} and completeness.get("status") != "current_facts_sparse":
        return response
    answer = response.answer or ""
    if re.search(r"只看到|只能看到|不能判断|没法判断|未确认|低置信|线索|资料不足|证据", answer):
        return response
    prefix = "我先不判断业主心态，只给低风险开口：现在看不到业主特征卡和完整沟通记录，别急着猜他的价格心理。"
    response.answer = f"{prefix}\n\n{answer}"
    if response.dialogueDigest is not None:
        response.dialogueDigest.discussionSummary = (
            "当前业主资料不足，SuFen 已先补充证据边界，再给低风险沟通话术。"
        )
    response.toolAudit.append(
        ToolAuditItem(
            tool="provider.postprocess",
            action="prefix_property_owner_boundary",
            status="ok",
        )
    )
    return response


def _final_answer_guardrail(
    response: SuFenResponse,
    *,
    task: SuFenTaskPackage,
    prompt: str,
) -> SuFenResponse:
    intent = _classify_user_intent(prompt, task)
    raw_user_message = _raw_user_message(prompt, task)
    if intent in {"casual_greeting", "casual_ack"}:
        response.answer = _short_casual_answer(raw_user_message, task)
        response.dialogueDigest = DialogueDigest(
            coreIntent="用户做简短寒暄或确认",
            discussionSummary="SuFen 只做简短回应，没有展开档案事实、资料缺口、图谱状态或业务判断。",
            finalOutcome="已简短回应，不形成业务结论。",
            userAcceptance="chat",
            subjectRelevance=DialogueSubjectRelevance(
                level="none",
                shouldPersist=False,
                reason="寒暄、问候或简单确认，不进入当前档案日志。",
            ),
        )
        response.evidenceUsed = []
        response.missingAuthorizationRequests = []
        response.eventDrafts = []
        response.fieldPatchDrafts = []
        response.memoryPatch = None
        response.toolAudit.append(
            ToolAuditItem(
                tool="provider.output_guardrail",
                action="casual_short_answer",
                status="ok",
            )
        )
        return response

    answer = response.answer or ""
    if "目前只能低置信度处理" in answer:
        response.answer = answer.replace("目前只能低置信度处理：", "资料还薄，我先收住判断：")
        response.toolAudit.append(
            ToolAuditItem(
                tool="provider.output_guardrail",
                action="naturalize_confidence_prefix",
                status="ok",
            )
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
    if name == "sufen_memory_search":
        for key in _task_scope(task):
            clean.pop(key, None)
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


def provider_fail_closed_response(reason: str, status: str) -> SuFenResponse:
    return SuFenResponse(
        answer=FAIL_CLOSED_MESSAGE,
        dialogueDigest=DialogueDigest(
            coreIntent="SuFen provider 或工具链未能完成本轮受控回答",
            discussionSummary=f"SuFen 因 {reason} 进入 fail-closed 分支，没有把不完整资料包装成业务判断。",
            finalOutcome="本轮未形成可入档的业务结论，需要重新获取资料或恢复 provider/tool 配置后再判断。",
            userAcceptance="unclear",
            subjectRelevance=DialogueSubjectRelevance(level="none", shouldPersist=False, reason=f"fail-closed: {status}"),
        ),
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
    retried_authorized_context = False
    for turn in range(1, MAX_TOOL_LOOP_TURNS + 1):
        data = _request_provider(settings, messages, prompt, task)
        message = _message_from_provider(data)
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            response = _provider_message_to_sufen(message)
            if _looks_like_wrong_missing_context_response(response, task):
                if not retried_authorized_context:
                    retried_authorized_context = True
                    loop_audit.append(
                        ToolAuditItem(
                            tool="provider.chat_completions",
                            action="authorized_context_retry",
                            status="model_requested_missing_data_despite_task_package",
                        )
                    )
                    prompt = _authorized_context_retry_prompt(prompt, response, task)
                    messages = build_provider_messages(prompt, task)
                    continue
                response = _authorized_context_fallback_response(
                    prompt=prompt,
                    previous_response=response,
                    task=task,
                    loop_audit=loop_audit,
                )
                response = _compact_sparse_property_answer_if_needed(response, task=task, prompt=prompt)
                response = _prefix_property_owner_boundary_if_needed(response, task=task, prompt=prompt)
                response = _final_answer_guardrail(response, task=task, prompt=prompt)
                return response
            response = _compact_sparse_property_answer_if_needed(response, task=task, prompt=prompt)
            response = _prefix_property_owner_boundary_if_needed(response, task=task, prompt=prompt)
            response = _final_answer_guardrail(response, task=task, prompt=prompt)
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
            if _has_backend_authorized_context(task):
                response = _authorized_context_fallback_response(
                    prompt=prompt,
                    previous_response=response,
                    task=task,
                    loop_audit=loop_audit,
                )
                response = _compact_sparse_property_answer_if_needed(response, task=task, prompt=prompt)
                response = _prefix_property_owner_boundary_if_needed(response, task=task, prompt=prompt)
                response = _final_answer_guardrail(response, task=task, prompt=prompt)
                return response
            return response

    response = provider_fail_closed_response("tool_loop_exceeded", f"max_turns:{MAX_TOOL_LOOP_TURNS}")
    response.toolAudit.extend(loop_audit)
    if _has_backend_authorized_context(task):
        response = _authorized_context_fallback_response(
            prompt=prompt,
            previous_response=response,
            task=task,
            loop_audit=loop_audit,
        )
        response = _compact_sparse_property_answer_if_needed(response, task=task, prompt=prompt)
        response = _prefix_property_owner_boundary_if_needed(response, task=task, prompt=prompt)
        response = _final_answer_guardrail(response, task=task, prompt=prompt)
    return response
