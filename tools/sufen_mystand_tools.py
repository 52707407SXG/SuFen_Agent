"""SuFen My Stand tool pack.

First release is adapter-safe: no production database access and no direct
writes. Draft tools only return structured drafts for My Stand to review.
"""

from __future__ import annotations

import difflib
import json
from typing import Any

from sufen.auth import extract_authorization_refs, fail_closed, refs_to_dicts
from sufen.memory import draft_memory_patch, load_memory, memory_path
from sufen.task_package import SuFenTaskPackage
from tools.registry import registry


def _ok(payload: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, **payload}


def _task_scope(task: SuFenTaskPackage) -> dict[str, str]:
    archive = task.archiveContext or {}
    return {
        "companyId": str(archive.get("companyId") or "company-ZYJ"),
        "operatorUserId": task.operator.userId,
        "subjectType": task.subject.type,
        "subjectId": task.subject.id,
    }


def _require_task(task_package: SuFenTaskPackage | None) -> SuFenTaskPackage | dict[str, Any]:
    if task_package is None:
        return fail_closed("missing_task_package")
    return task_package


def _scope_mismatch(args: dict[str, Any], scope: dict[str, str]) -> str | None:
    for key, expected in scope.items():
        if key in args and args.get(key) not in (None, "", expected):
            return key
    nested = args.get("scope")
    if isinstance(nested, dict):
        for key, expected in scope.items():
            if key in nested and nested.get(key) not in (None, "", expected):
                return f"scope.{key}"
    return None


def _archive_authorization_ids(task: SuFenTaskPackage) -> set[str]:
    refs = {f"AUTH-{task.subject.id}", f"OUT-{task.subject.id}", f"ref_{task.subject.id}"}
    archive = task.archiveContext or {}
    direct_keys = (
        "authorizationId",
        "authorizationIds",
        "authorizedRef",
        "authorizedRefs",
        "authRef",
        "authRefs",
        "sourceRef",
        "sourceRefs",
    )
    for key in direct_keys:
        value = archive.get(key)
        if isinstance(value, str):
            refs.add(value)
        elif isinstance(value, list):
            refs.update(str(item) for item in value if item)
    encoded = json.dumps(archive, ensure_ascii=False, default=str)
    refs.update(ref.raw for ref in extract_authorization_refs(encoded) if ref.kind != "knowledge-graph")
    return {ref for ref in refs if ref}


def _resolve(args: dict[str, Any], **_: Any) -> dict[str, Any]:
    refs = refs_to_dicts(extract_authorization_refs(args.get("text", "")))
    return _ok({"refs": refs, "requiresBackendAuthorization": True})


def _archive_read(args: dict[str, Any], *, task_package: SuFenTaskPackage | None = None, **_: Any) -> dict[str, Any]:
    task = _require_task(task_package)
    if isinstance(task, dict):
        return task
    auth_id = (args.get("authorizationId") or "").strip()
    if not auth_id:
        return fail_closed()
    if auth_id not in _archive_authorization_ids(task):
        return fail_closed("unauthorized_archive_ref")
    return _ok({
        "authorizationId": auth_id,
        "archive": task.archiveContext or {},
        "note": "Read from the current My Stand taskPackage archiveContext. Model supplied payload is ignored.",
    })


def _kg_read(args: dict[str, Any], *, task_package: SuFenTaskPackage | None = None, **_: Any) -> dict[str, Any]:
    task = _require_task(task_package)
    if isinstance(task, dict):
        return task
    ref = (args.get("knowledgeGraphRef") or "").strip()
    if not ref:
        return fail_closed("missing_knowledge_graph_ref")
    if ref not in set(task.knowledgeGraphRefs):
        return fail_closed("unauthorized_knowledge_graph_ref")
    graphs = task.archiveContext.get("knowledgeGraphs") if isinstance(task.archiveContext, dict) else None
    graph = graphs.get(ref) if isinstance(graphs, dict) else {}
    return _ok({
        "knowledgeGraphRef": ref,
        "graph": graph or {"ref": ref, "scene": task.scene},
        "note": "Read from the current My Stand taskPackage knowledgeGraphRefs. Model supplied payload is ignored.",
    })


def _parse(args: dict[str, Any], **_: Any) -> dict[str, Any]:
    text = args.get("text", "")
    return _ok({"refs": refs_to_dicts(extract_authorization_refs(text)), "textLength": len(text)})


def _memory_search(args: dict[str, Any], *, task_package: SuFenTaskPackage | None = None, **_: Any) -> dict[str, Any]:
    task = _require_task(task_package)
    if isinstance(task, dict):
        return task
    scope = _task_scope(task)
    mismatch = _scope_mismatch(args, scope)
    if mismatch:
        return fail_closed(f"memory_scope_mismatch_{mismatch}")
    try:
        path = memory_path(
            company_id=scope["companyId"],
            operator_user_id=scope["operatorUserId"],
            subject_type=scope["subjectType"],
            subject_id=scope["subjectId"],
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


def _memory_patch_draft(args: dict[str, Any], *, task_package: SuFenTaskPackage | None = None, **_: Any) -> dict[str, Any]:
    task = _require_task(task_package)
    if isinstance(task, dict):
        return task
    scope = _task_scope(task)
    mismatch = _scope_mismatch(args, scope)
    if mismatch:
        return fail_closed(f"memory_scope_mismatch_{mismatch}")
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
    }, ["authorizationId"]),
    handler=_archive_read,
    emoji="📁",
)
registry.register(
    name="mystand.knowledge_graph.read",
    toolset="sufen",
    schema=_schema("mystand.knowledge_graph.read", "Read an already-authorized My Stand knowledge graph payload.", {
        "knowledgeGraphRef": {"type": "string"},
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
    schema=_schema("sufen_memory_search", "Search scoped SuFen memory for the current taskPackage operator and subject.", {
        "query": {"type": "string"},
    }),
    handler=_memory_search,
    emoji="🧠",
)
registry.register(
    name="sufen_memory_patch_draft",
    toolset="sufen",
    schema=_schema("sufen_memory_patch_draft", "Create a scoped memory patch draft. Does not write memory.", {
        "patch": {"type": "object"},
    }, ["patch"]),
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
