"""My Stand task package models for SuFen."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


AllowedAction = Literal["analyze", "suggest", "eventDraft", "fieldPatchDraft", "memoryPatch"]
DeniedAction = Literal["directWrite", "crossUserRead", "externalSend", "rawDbAccess"]


class Operator(BaseModel):
    userId: str
    name: str | None = None
    role: str | None = None
    isGangGe: bool = False


class Subject(BaseModel):
    type: Literal["property", "client", "after-sale", "broker"]
    id: str


class AgentDelegationToken(BaseModel):
    issuer: Literal["mystand-core"] = "mystand-core"
    audience: Literal["sufen-agent"] = "sufen-agent"
    actorAgent: str
    operatorUserId: str
    subject: Subject
    allowedActions: list[AllowedAction]
    expiresAt: str
    nonce: str
    signature: str


class SuFenTaskPackage(BaseModel):
    operator: Operator
    subject: Subject
    scene: str
    archiveContext: dict[str, Any] = Field(default_factory=dict)
    brokerProfile: dict[str, Any] = Field(default_factory=dict)
    knowledgeGraphRefs: list[str] = Field(default_factory=list)
    scopedMemoryKey: str | None = None
    allowedActions: list[AllowedAction] = Field(default_factory=lambda: [
        "analyze",
        "suggest",
        "eventDraft",
        "fieldPatchDraft",
        "memoryPatch",
    ])
    deniedActions: list[DeniedAction] = Field(default_factory=lambda: [
        "directWrite",
        "crossUserRead",
        "externalSend",
        "rawDbAccess",
    ])
    delegationToken: AgentDelegationToken | None = None


def ensure_safe_actions(task: SuFenTaskPackage) -> None:
    forbidden = {"directWrite", "crossUserRead", "externalSend", "rawDbAccess"}
    if not forbidden.issubset(set(task.deniedActions)):
        missing = sorted(forbidden.difference(task.deniedActions))
        raise ValueError(f"task package is missing deniedActions: {', '.join(missing)}")

    token = task.delegationToken
    if token is None:
        return
    if token.operatorUserId != task.operator.userId:
        raise ValueError("delegation token operatorUserId does not match task operator")
    if token.subject.model_dump() != task.subject.model_dump():
        raise ValueError("delegation token subject does not match task subject")
    missing_allowed = set(task.allowedActions).difference(token.allowedActions)
    if missing_allowed:
        raise ValueError(f"delegation token does not allow actions: {', '.join(sorted(missing_allowed))}")
    if not token.signature.strip():
        raise ValueError("delegation token signature is required")
