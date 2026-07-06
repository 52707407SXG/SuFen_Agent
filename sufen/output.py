"""Structured response contract for SuFen."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class EvidenceItem(BaseModel):
    source: str
    summary: str
    confidence: float = Field(ge=0, le=1, default=0.5)


class AuthorizationRequest(BaseModel):
    reason: str
    acceptableRefs: list[str] = Field(default_factory=list)
    message: str


class EventDraft(BaseModel):
    name: str
    body: str
    eventTime: str | None = None
    remindTime: str | None = None
    repeatType: str | None = None
    priority: Literal["low", "normal", "high"] = "normal"
    target: dict[str, Any] = Field(default_factory=dict)
    reason: str
    draftOnly: bool = True


class FieldPatchDraft(BaseModel):
    target: dict[str, Any] = Field(default_factory=dict)
    field: str
    before: Any = None
    after: Any = None
    diff: str
    reason: str
    draftOnly: bool = True


class MemoryPatch(BaseModel):
    scope: dict[str, Any] = Field(default_factory=dict)
    businessFacts: list[str] = Field(default_factory=list)
    strategyObservations: list[str] = Field(default_factory=list)
    brokerAdaptation: list[str] = Field(default_factory=list)
    openQuestions: list[str] = Field(default_factory=list)
    lastSummary: str | None = None
    memoryIndexText: str | None = None
    sourceRefs: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1, default=0.5)
    draftOnly: bool = True


class ToolAuditItem(BaseModel):
    tool: str
    action: str
    status: str
    draftOnly: bool = True


class SuFenResponse(BaseModel):
    answer: str
    evidenceUsed: list[EvidenceItem] = Field(default_factory=list)
    missingAuthorizationRequests: list[AuthorizationRequest] = Field(default_factory=list)
    eventDrafts: list[EventDraft] = Field(default_factory=list)
    fieldPatchDrafts: list[FieldPatchDraft] = Field(default_factory=list)
    memoryPatch: MemoryPatch | None = None
    toolAudit: list[ToolAuditItem] = Field(default_factory=list)


def empty_response(answer: str) -> SuFenResponse:
    return SuFenResponse(answer=answer)
