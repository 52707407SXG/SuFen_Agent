"""My Stand task package models for SuFen."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import threading
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


_NONCE_LOCK = threading.Lock()
_USED_NONCES: set[str] = set()


def clear_delegation_nonce_cache() -> None:
    with _NONCE_LOCK:
        _USED_NONCES.clear()


def _parse_expires_at(value: str) -> datetime:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("delegation token expiresAt must be ISO-8601") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _signature_payload(token: AgentDelegationToken) -> dict[str, Any]:
    return {
        "issuer": token.issuer,
        "audience": token.audience,
        "actorAgent": token.actorAgent,
        "operatorUserId": token.operatorUserId,
        "subject": token.subject.model_dump(mode="json"),
        "allowedActions": list(token.allowedActions),
        "expiresAt": token.expiresAt,
        "nonce": token.nonce,
    }


def canonical_delegation_payload(token: AgentDelegationToken) -> str:
    return json.dumps(_signature_payload(token), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sign_delegation_token(token: AgentDelegationToken, secret: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        canonical_delegation_payload(token).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"hmac-sha256:{digest}"


def _normalize_signature(signature: str) -> str:
    value = signature.strip()
    if value.startswith("hmac-sha256:"):
        return value.split(":", 1)[1].strip()
    if value.startswith("sha256="):
        return value.split("=", 1)[1].strip()
    return value


def _delegation_secret(explicit: str | None) -> str:
    if explicit:
        return explicit.strip()
    try:
        from sufen.config import load_settings

        return load_settings().delegation_hmac_secret.strip()
    except Exception:
        return ""


def _verify_delegation_token(
    token: AgentDelegationToken,
    *,
    delegation_secret: str | None,
    now: datetime | None,
    consume_nonce: bool,
) -> None:
    if not token.nonce.strip():
        raise ValueError("delegation token nonce is required")
    if not token.signature.strip():
        raise ValueError("delegation token signature is required")

    expires_at = _parse_expires_at(token.expiresAt)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if expires_at <= current:
        raise ValueError("delegation token has expired")

    secret = _delegation_secret(delegation_secret)
    if not secret:
        raise ValueError("SUFEN_DELEGATION_HMAC_SECRET is required to verify delegation token")

    expected = _normalize_signature(sign_delegation_token(token, secret))
    supplied = _normalize_signature(token.signature)
    if not hmac.compare_digest(supplied, expected):
        raise ValueError("delegation token signature is invalid")

    if consume_nonce:
        nonce_key = f"{token.issuer}:{token.audience}:{token.nonce}"
        with _NONCE_LOCK:
            if nonce_key in _USED_NONCES:
                raise ValueError("delegation token nonce has already been used")
            _USED_NONCES.add(nonce_key)


def ensure_safe_actions(
    task: SuFenTaskPackage,
    *,
    delegation_secret: str | None = None,
    now: datetime | None = None,
    consume_nonce: bool = True,
) -> None:
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
    _verify_delegation_token(
        token,
        delegation_secret=delegation_secret,
        now=now,
        consume_nonce=consume_nonce,
    )
