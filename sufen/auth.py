"""Authorization reference parsing and fail-closed helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


AUTH_REF_RE = re.compile(
    r"(?P<ref>(?:AUTH|OUT|KGREF)-[A-Za-z0-9][A-Za-z0-9_.:-]*|ref_[A-Za-z0-9][A-Za-z0-9_.:-]*|knowledge:[A-Za-z0-9][A-Za-z0-9_.:/-]*)"
)

FAIL_CLOSED_MESSAGE = (
    "这个问题值得拆一下，但我现在缺关键资料。你可以在对应档案或知识图谱里点"
    "“站内ID”，打开可读，把站内ID复制给我；我拿到可读资料后再按真实内容帮你判断。"
)


@dataclass(frozen=True)
class AuthorizationRef:
    raw: str
    kind: str


def extract_authorization_refs(text: str | None) -> list[AuthorizationRef]:
    refs: list[AuthorizationRef] = []
    if not text:
        return refs
    for match in AUTH_REF_RE.finditer(text):
        raw = match.group("ref")
        if raw.startswith("AUTH-"):
            kind = "archive-auth"
        elif raw.startswith("OUT-"):
            kind = "outbound-auth"
        elif raw.startswith("KGREF-") or raw.startswith("knowledge:"):
            kind = "knowledge-graph"
        else:
            kind = "reference"
        refs.append(AuthorizationRef(raw=raw, kind=kind))
    return refs


def refs_to_dicts(refs: Iterable[AuthorizationRef]) -> list[dict[str, str]]:
    return [{"id": ref.raw, "kind": ref.kind} for ref in refs]


def fail_closed(reason: str = "missing_authorized_reference") -> dict:
    return {
        "ok": False,
        "status": "authorization_required",
        "reason": reason,
        "message": FAIL_CLOSED_MESSAGE,
    }
