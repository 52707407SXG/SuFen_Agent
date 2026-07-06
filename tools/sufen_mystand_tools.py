"""SuFen My Stand tool pack.

First release is adapter-safe: no production database access and no direct
writes. Draft tools only return structured drafts for My Stand to review.
"""

from __future__ import annotations

import difflib
from typing import Any

from sufen.auth import extract_authorization_refs, fail_closed, refs_to_dicts
from sufen.memory import draft_memory_patch, load_memory, memory_path
from tools.registry import registry


def _ok(payload: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, **payload}


def _resolve(args: dict[str, Any], **_: Any) -> dict[str, Any]:
    refs = refs_to_dicts(extract_authorization_refs(args.get("text", "")))
    return _ok({"refs": refs, "requiresBackendAuthorization": True})


def _archive_read(args: dict[str, Any], **_: Any) -> dict[str, Any]:
    auth_id = (args.get("authorizationId") or "").strip()
    if not auth_id:
        return fail_closed()
    return _ok({
        "authorizationId": auth_id,
        "archive": args.get("authorizedPayload") or {},
        "note": "No production database is connected. My Stand must inject authorizedPayload.",
    })


def _kg_read(args: dict[str, Any], **_: Any) -> dict[str, Any]:
    ref = (args.get("knowledgeGraphRef") or "").strip()
    if not ref:
        return fail_closed("missing_knowledge_graph_ref")
    return _ok({
        "knowledgeGraphRef": ref,
        "graph": args.get("authorizedPayload") or {},
        "note": "No production knowledge graph is connected. My Stand must inject authorizedPayload.",
    })


def _parse(args: dict[str, Any], **_: Any) -> dict[str, Any]:
    text = args.get("text", "")
    return _ok({"refs": refs_to_dicts(extract_authorization_refs(text)), "textLength": len(text)})


def _memory_search(args: dict[str, Any], **_: Any) -> dict[str, Any]:
    try:
        path = memory_path(
            company_id=args.get("companyId", "company-ZYJ"),
            operator_user_id=args["operatorUserId"],
            subject_type=args["subjectType"],
            subject_id=args["subjectId"],
            root=args.get("memoryRoot"),
            admin=bool(args.get("admin", False)),
        )
    except KeyError as exc:
        return fail_closed(f"missing_memory_scope_{exc.args[0]}")
    memory = load_memory(path)
    query = (args.get("query") or "").lower()
    haystack = " ".join([
        memory.get("memoryIndexText") or "",
        " ".join(memory.get("businessFacts") or []),
        " ".join(memory.get("strategyObservations") or []),
        " ".join(memory.get("openQuestions") or []),
    ]).lower()
    return _ok({
        "path": str(path),
        "memory": memory,
        "matched": bool(query and query in haystack),
    })


def _memory_patch_draft(args: dict[str, Any], **_: Any) -> dict[str, Any]:
    scope = args.get("scope") or {}
    patch = args.get("patch") or {}
    return _ok({"memoryPatch": draft_memory_patch(scope, patch)})


def _event_draft(args: dict[str, Any], **_: Any) -> dict[str, Any]:
    draft = {
        "name": args.get("name") or "SuFen 跟进建议",
        "body": args.get("body") or "",
        "eventTime": args.get("eventTime"),
        "remindTime": args.get("remindTime"),
        "repeatType": args.get("repeatType"),
        "priority": args.get("priority", "normal"),
        "target": args.get("target") or {},
        "reason": args.get("reason") or "SuFen strategy suggestion",
        "draftOnly": True,
    }
    return _ok({"eventDraft": draft})


def _field_patch_draft(args: dict[str, Any], **_: Any) -> dict[str, Any]:
    before = "" if args.get("before") is None else str(args.get("before"))
    after = "" if args.get("after") is None else str(args.get("after"))
    diff = "\n".join(difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        fromfile="before",
        tofile="after",
        lineterm="",
    ))
    return _ok({
        "fieldPatchDraft": {
            "target": args.get("target") or {},
            "field": args.get("field") or "",
            "before": args.get("before"),
            "after": args.get("after"),
            "diff": diff,
            "reason": args.get("reason") or "SuFen field patch suggestion",
            "draftOnly": True,
        }
    })


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required or [],
        },
    }


registry.register(
    name="mystand.auth.resolve",
    toolset="sufen",
    schema=_schema("mystand.auth.resolve", "Resolve SuFen authorization and knowledge refs from text.", {"text": {"type": "string"}}, ["text"]),
    handler=_resolve,
    emoji="🔐",
)
registry.register(
    name="mystand.archive.read",
    toolset="sufen",
    schema=_schema("mystand.archive.read", "Read an already-authorized My Stand archive payload.", {
        "authorizationId": {"type": "string"},
        "authorizedPayload": {"type": "object"},
    }, ["authorizationId"]),
    handler=_archive_read,
    emoji="📁",
)
registry.register(
    name="mystand.knowledge_graph.read",
    toolset="sufen",
    schema=_schema("mystand.knowledge_graph.read", "Read an already-authorized My Stand knowledge graph payload.", {
        "knowledgeGraphRef": {"type": "string"},
        "authorizedPayload": {"type": "object"},
    }, ["knowledgeGraphRef"]),
    handler=_kg_read,
    emoji="🧭",
)
registry.register(
    name="mystand_parse",
    toolset="sufen",
    schema=_schema("mystand_parse", "Parse My Stand archive refs and knowledge refs from text.", {"text": {"type": "string"}}, ["text"]),
    handler=_parse,
    emoji="🧩",
)
registry.register(
    name="sufen_memory_search",
    toolset="sufen",
    schema=_schema("sufen_memory_search", "Search scoped SuFen memory for one operator and one subject.", {
        "companyId": {"type": "string"},
        "operatorUserId": {"type": "string"},
        "subjectType": {"type": "string"},
        "subjectId": {"type": "string"},
        "query": {"type": "string"},
        "memoryRoot": {"type": "string"},
        "admin": {"type": "boolean"},
    }, ["operatorUserId", "subjectType", "subjectId"]),
    handler=_memory_search,
    emoji="🧠",
)
registry.register(
    name="sufen_memory_patch_draft",
    toolset="sufen",
    schema=_schema("sufen_memory_patch_draft", "Create a scoped memory patch draft. Does not write memory.", {
        "scope": {"type": "object"},
        "patch": {"type": "object"},
    }, ["scope", "patch"]),
    handler=_memory_patch_draft,
    emoji="📝",
)
registry.register(
    name="mystand.event.draft",
    toolset="sufen",
    schema=_schema("mystand.event.draft", "Create an event draft for My Stand UI review.", {
        "name": {"type": "string"},
        "body": {"type": "string"},
        "eventTime": {"type": "string"},
        "remindTime": {"type": "string"},
        "repeatType": {"type": "string"},
        "priority": {"type": "string"},
        "target": {"type": "object"},
        "reason": {"type": "string"},
    }),
    handler=_event_draft,
    emoji="📌",
)
registry.register(
    name="mystand.field_patch_draft",
    toolset="sufen",
    schema=_schema("mystand.field_patch_draft", "Create a before/after/diff field patch draft for My Stand UI review.", {
        "target": {"type": "object"},
        "field": {"type": "string"},
        "before": {},
        "after": {},
        "reason": {"type": "string"},
    }, ["field", "before", "after"]),
    handler=_field_patch_draft,
    emoji="🧾",
)
