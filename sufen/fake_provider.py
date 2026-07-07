"""Deterministic fake provider used by smoke tests and local dry-runs."""

from __future__ import annotations

from sufen.auth import FAIL_CLOSED_MESSAGE, extract_authorization_refs, refs_to_dicts
from sufen.output import AuthorizationRequest, EvidenceItem, SuFenResponse, ToolAuditItem
from sufen.property_strategy import build_property_archive_response
from sufen.task_package import SuFenTaskPackage, ensure_safe_actions


def answer_with_fake_provider(prompt: str, task: SuFenTaskPackage | None = None) -> SuFenResponse:
    refs = extract_authorization_refs(prompt)
    tool_audit = [ToolAuditItem(tool="mystand_parse", action="extract_authorization_refs", status="ok")]
    if task is not None:
        ensure_safe_actions(task)
        tool_audit.append(ToolAuditItem(tool="task_package", action="validate_scope", status="ok"))
        if task.subject.type == "property":
            return build_property_archive_response(
                prompt=prompt,
                refs=refs,
                task=task,
                initial_audit=tool_audit,
            )

    if not refs and task is None:
        return SuFenResponse(
            answer=FAIL_CLOSED_MESSAGE,
            missingAuthorizationRequests=[
                AuthorizationRequest(
                    reason="missing_authorized_reference",
                    acceptableRefs=["AUTH-...", "OUT-...", "KGREF-...", "ref_...", "knowledge:..."],
                    message=FAIL_CLOSED_MESSAGE,
                )
            ],
            toolAudit=tool_audit,
        )

    evidence = [
        EvidenceItem(
            source=ref["id"],
            summary=f"识别到 {ref['kind']} 资料钥匙，等待 My Stand 后端提供授权内容。",
            confidence=0.8,
        )
        for ref in refs_to_dicts(refs)
    ]
    subject_label = ""
    if task is not None:
        subject_label = f" 当前档案：{task.subject.type}/{task.subject.id}。"
    return SuFenResponse(
        answer=(
            "我已按 SuFen 的资料优先规则进入分析模式。"
            f"{subject_label}第一版本地 dry-run 不连接生产库；拿到授权档案、知识图谱和经纪人特征卡后，"
            "我会输出策略建议、事件草稿和字段 diff 草稿；memoryPatch 保持为空。"
        ),
        evidenceUsed=evidence,
        toolAudit=tool_audit,
    )
