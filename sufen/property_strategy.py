"""First-release property archive strategy output for SuFen."""

from __future__ import annotations

import difflib
from typing import Any, Iterable

from sufen.auth import AuthorizationRef, FAIL_CLOSED_MESSAGE
from sufen.output import (
    AuthorizationRequest,
    EventDraft,
    EvidenceItem,
    FieldPatchDraft,
    SuFenResponse,
    ToolAuditItem,
)
from sufen.task_package import SuFenTaskPackage


def _pick(data: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        if key in data and data[key] not in (None, "", [], {}):
            return data[key]
    return default


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "；".join(_text(item) for item in value if _text(item))
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            if item not in (None, "", [], {}):
                parts.append(f"{key}: {_text(item)}")
        return "；".join(parts)
    return str(value).strip()


def _diff(before: Any, after: Any) -> str:
    before_text = _text(before)
    after_text = _text(after)
    return "\n".join(
        difflib.unified_diff(
            before_text.splitlines(),
            after_text.splitlines(),
            fromfile="before",
            tofile="after",
            lineterm="",
        )
    )


def _broker_adaptation(task: SuFenTaskPackage) -> str:
    profile = task.brokerProfile or {}
    stage = _text(_pick(profile, "capabilityStage", "stage", "level", default=""))
    traits = _text(_pick(profile, "traits", "featureCard", "style", default=""))
    if "新手" in stage or "new" in stage.lower():
        return "按新手经纪人处理：拆步骤、给话术、给检查点，避免只给抽象判断。"
    if "高手" in stage or "senior" in stage.lower() or "expert" in stage.lower():
        return "按高手经纪人处理：直接指出关键矛盾、成交风险和可选策略。"
    if "懒" in traits:
        return "按执行松散型处理：给明确动作、截止点和复盘口径。"
    if "情绪" in traits:
        return "按情绪化经纪人处理：先稳情绪，再给可执行动作。"
    if "复杂" in traits:
        return "按复杂思维型处理：给推演依据和备选路径。"
    return "按当前经纪人特征卡处理：建议保持可执行、可复盘、可确认。"


def _source_refs(refs: Iterable[AuthorizationRef], task: SuFenTaskPackage) -> list[str]:
    out = [ref.raw for ref in refs]
    out.extend(task.knowledgeGraphRefs)
    return list(dict.fromkeys(item for item in out if item))


def build_property_archive_response(
    *,
    prompt: str,
    refs: list[AuthorizationRef],
    task: SuFenTaskPackage,
    initial_audit: list[ToolAuditItem],
) -> SuFenResponse:
    """Build the first SuFen property archive scenario from a task package.

    This is deliberately deterministic. It proves the My Stand contract without
    touching production data: all archive, knowledge, broker, score, event, and
    memory context must arrive inside the task package or authorized refs.
    """

    archive = task.archiveContext or {}
    base = _pick(archive, "baseInfo", "property", "basicInfo", default={})
    if not isinstance(base, dict):
        base = {"summary": base}
    five_scores = _pick(
        archive,
        "fiveDimensionScores",
        "fiveScores",
        "score5d",
        "scores",
        default={},
    )
    event_summary = _pick(
        archive,
        "eventSummary",
        "eventSummaries",
        "eventsSummary",
        "recentEvents",
        default=[],
    )
    property_note = _pick(
        archive,
        "propertyNote",
        "archiveNote",
        "ownerNote",
        "strategyNote",
        "note",
        default="",
    )
    source_refs = _source_refs(refs, task)

    missing_requests: list[AuthorizationRequest] = []
    has_archive_material = bool(archive or any(ref.raw.startswith(("AUTH-", "OUT-", "ref_")) for ref in refs))
    if not has_archive_material:
        missing_requests.append(
            AuthorizationRequest(
                reason="missing_authorized_archive",
                acceptableRefs=["AUTH-...", "OUT-...", "ref_..."],
                message=FAIL_CLOSED_MESSAGE,
            )
        )
    if task.scene in {"房源维护", "property-maintenance"} and not task.knowledgeGraphRefs and not any(
        ref.raw.startswith(("KGREF-", "knowledge:")) for ref in refs
    ):
        missing_requests.append(
            AuthorizationRequest(
                reason="missing_property_knowledge_graph",
                acceptableRefs=["KGREF-...", "knowledge:..."],
                message=FAIL_CLOSED_MESSAGE,
            )
        )

    if missing_requests and not archive:
        return SuFenResponse(
            answer=FAIL_CLOSED_MESSAGE,
            missingAuthorizationRequests=missing_requests,
            toolAudit=initial_audit,
        )

    confidence = 0.74 if not missing_requests else 0.46
    broker_adaptation = _broker_adaptation(task)
    title = _text(_pick(base, "title", "name", "address", default=task.subject.id)) or task.subject.id
    owner_signal = _text(_pick(archive, "ownerIntent", "ownerSignal", "ownerStatus", default="业主真实动机待继续确认"))
    price_signal = _text(_pick(base, "askingPrice", "price", "totalPrice", default="价格信息待确认"))
    scores_text = _text(five_scores) or "五维评分未完整注入"
    events_text = _text(event_summary) or "近期事件摘要未完整注入"
    note_text = _text(property_note)

    strategy_after = (
        f"SuFen策略建议：围绕「{title}」先确认业主真实目标和价格底线；"
        f"当前价格信号：{price_signal}；业主信号：{owner_signal}；"
        f"五维评分参考：{scores_text}。下一步先用一次结构化回访补齐未知项，再决定是否推降价、换卖点或加强带看反馈。"
    )
    answer = (
        f"基于已注入的房源档案、经纪人特征卡、入口指定知识图谱和只读人工 memory，SuFen 的初步判断是："
        f"这套房源当前要先验证业主真实意图和价格弹性，不能只按一句话做字面跟进。{broker_adaptation}"
        f"关键依据：{events_text}。建议先做一次回访确认，再把确认结果交给 My Stand 前端审查后写入。"
    )

    event_drafts: list[EventDraft] = []
    if "eventDraft" in task.allowedActions:
        event_drafts.append(
            EventDraft(
                name="SuFen 房源维护回访",
                body=(
                    f"回访 {title} 业主：确认出售动机、价格底线、近期看房反馈和下一步维护动作。"
                    f"经纪人执行方式：{broker_adaptation}"
                ),
                repeatType="none",
                priority="high" if confidence >= 0.7 else "normal",
                target={"type": task.subject.type, "id": task.subject.id, "operatorUserId": task.operator.userId},
                reason="首版业主房源档案场景要求生成事件草稿；仅预填，等待 My Stand 用户确认。",
            )
        )

    field_patch_drafts: list[FieldPatchDraft] = []
    if "fieldPatchDraft" in task.allowedActions:
        field_patch_drafts.append(
            FieldPatchDraft(
                target={"type": task.subject.type, "id": task.subject.id},
                field="strategyNote",
                before=note_text,
                after=strategy_after,
                diff=_diff(note_text, strategy_after),
                reason="根据档案上下文、五维评分、事件摘要和知识图谱引用生成字段修改草稿；不直接写库。",
            )
        )

    evidence = [
        EvidenceItem(source="task.archiveContext", summary="已读取 My Stand 注入的授权房源档案上下文。", confidence=confidence),
        EvidenceItem(source="task.brokerProfile", summary=f"已读取经纪人特征卡：{broker_adaptation}", confidence=confidence),
        EvidenceItem(source="task.archiveContext.fiveDimensionScores", summary=scores_text, confidence=confidence),
        EvidenceItem(source="task.archiveContext.eventSummary", summary=events_text, confidence=confidence),
    ]
    evidence.extend(
        EvidenceItem(source=ref.raw, summary=f"识别到 {ref.kind} 资料钥匙。", confidence=0.8)
        for ref in refs
    )
    evidence.extend(
        EvidenceItem(source=kg_ref, summary="已作为房源维护知识图谱引用纳入本轮判断。", confidence=confidence)
        for kg_ref in task.knowledgeGraphRefs
    )
    audit = list(initial_audit)
    audit.extend(
        [
            ToolAuditItem(tool="mystand.archive.read", action="consume_task_archiveContext", status="ok" if archive else "missing"),
            ToolAuditItem(tool="mystand.knowledge_graph.read", action="consume_knowledgeGraphRefs", status="ok" if task.knowledgeGraphRefs else "missing"),
            ToolAuditItem(tool="sufen_memory_search", action="read_human_memory_root", status="available_read_only"),
        ]
    )
    if event_drafts:
        audit.append(ToolAuditItem(tool="mystand.event.draft", action="create_draft", status="ok"))
    if field_patch_drafts:
        audit.append(ToolAuditItem(tool="mystand.field_patch_draft", action="create_diff_draft", status="ok"))
    return SuFenResponse(
        answer=answer,
        evidenceUsed=evidence,
        missingAuthorizationRequests=missing_requests,
        eventDrafts=event_drafts,
        fieldPatchDrafts=field_patch_drafts,
        memoryPatch=None,
        toolAudit=audit,
    )
