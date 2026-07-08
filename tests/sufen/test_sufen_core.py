from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from sufen.memory import iter_memory_documents, memory_root, search_human_memory
from sufen.output import SuFenResponse
from sufen.property_strategy import build_property_archive_response
from sufen.provider import (
    _normalize_provider_response_payload,
    _system_message,
    _task_bound_tool_args,
    build_provider_payload,
)
from sufen.task_package import (
    AgentDelegationToken,
    SuFenTaskPackage,
    clear_delegation_nonce_cache,
    ensure_safe_actions,
    sign_delegation_token,
)
from sufen.terminal_ui import TerminalPromptSession
from tools.registry import registry
import tools.sufen_mystand_tools  # noqa: F401
from toolsets import SUFEN_TOOL_NAMES


def make_task(**overrides) -> SuFenTaskPackage:
    data = {
        "operator": {"userId": "1001", "name": "经纪人A", "role": "broker"},
        "subject": {"type": "property", "id": "P-1"},
        "scene": "房源维护",
        "archiveContext": {
            "companyId": "company-ZYJ",
            "authorizationId": "AUTH-P-1",
            "archive": {"displayName": "阳光花园三居", "fields": {"业主姓名": "赵姐", "底价": "480万"}},
            "dialogueLogBrief": "暂无 SuFen 历史日志。",
            "requiredKnowledgeGraph": {
                "requiredName": "房源维护",
                "status": "available",
                "refId": "KGREF-property-maintenance",
            },
            "knowledgeGraphBinding": {
                "requiredName": "房源维护",
                "status": "available",
                "requiredRefId": "KGREF-property-maintenance",
            },
        },
        "brokerProfile": {"capabilityStage": "新手"},
        "knowledgeGraphRefs": ["KGREF-property-maintenance"],
        "dialogueLogKey": "company-ZYJ:1001:property:P-1",
        "requiredKnowledgeGraph": {
            "requiredName": "房源维护",
            "status": "available",
            "refId": "KGREF-property-maintenance",
        },
    }
    data.update(overrides)
    return SuFenTaskPackage.model_validate(data)


def test_task_defaults_disallow_memory_write() -> None:
    task = make_task()
    assert task.allowedActions == ["analyze", "suggest", "eventDraft", "fieldPatchDraft"]
    assert "memoryPatch" not in task.allowedActions
    assert "memoryWrite" in task.deniedActions
    assert task.dialogueLogKey == "company-ZYJ:1001:property:P-1"


def test_delegation_token_uses_new_allowed_actions() -> None:
    clear_delegation_nonce_cache()
    task = make_task()
    delegation = AgentDelegationToken.model_validate({
        "actorAgent": "mystand-core",
        "operatorUserId": "1001",
        "subject": task.subject.model_dump(mode="json"),
        "allowedActions": task.allowedActions,
        "expiresAt": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "nonce": "unit-test-token",
        "signature": "pending",
    })
    signed = delegation.model_copy(update={"signature": sign_delegation_token(delegation, "secret")})
    task.delegationToken = signed
    ensure_safe_actions(task, delegation_secret="secret")
    with pytest.raises(ValueError, match="nonce has already been used"):
        ensure_safe_actions(task, delegation_secret="secret")


def test_human_memory_root_is_read_only_and_not_scoped(tmp_path, monkeypatch) -> None:
    root = tmp_path / "memory"
    root.mkdir()
    (root / "MEMORY.md").write_text("# SuFen\n只记录刚哥和元老师维护的总规则。\n成都夏天注意节奏。", encoding="utf-8")
    (root / "ignore.bin").write_text("不可读", encoding="utf-8")
    (root / "company-ZYJ").mkdir()
    (root / "company-ZYJ" / "old.json").write_text('{"old": true}', encoding="utf-8")
    monkeypatch.setenv("SUFEN_MEMORY_ROOT", str(root))

    assert memory_root() == root
    docs = iter_memory_documents()
    assert [doc["name"] for doc in docs] == ["MEMORY.md"]
    result = search_human_memory("成都")
    assert result["mode"] == "single_human_memory_root_read_only"
    assert result["matches"][0]["name"] == "MEMORY.md"
    assert "write memory" in result["note"]


def test_toolset_removes_memory_patch_draft() -> None:
    assert "sufen_memory_search" in SUFEN_TOOL_NAMES
    assert "sufen_memory_patch_draft" not in SUFEN_TOOL_NAMES
    search_schema = registry.get_schema("sufen_memory_search")
    assert "single human" in search_schema["description"].lower()
    assert registry.get_schema("sufen_memory_patch_draft") is None


def test_memory_search_tool_ignores_model_scope_and_root(tmp_path, monkeypatch) -> None:
    root = tmp_path / "memory"
    root.mkdir()
    (root / "memory.md").write_text("房源维护要先听业主真实意图。", encoding="utf-8")
    monkeypatch.setenv("SUFEN_MEMORY_ROOT", str(root))
    task = make_task()
    result = registry.dispatch("sufen_memory_search", {
        "query": "真实意图",
        "companyId": "evil",
        "scope": {"operatorUserId": "evil"},
        "memoryRoot": "/tmp/evil",
        "admin": True,
    }, task_package=task)
    assert result["ok"] is True
    assert result["writeAllowed"] is False
    assert result["memory"]["root"] == str(root)
    assert result["memory"]["matches"][0]["name"] == "memory.md"


def test_provider_system_message_binds_knowledge_graph_and_logs() -> None:
    task = make_task()
    system = _system_message(task)
    assert "经纪人个人业务档案只用“经纪人成长路径”" in system
    assert "房源维护只用“房源维护”" in system
    assert "dialogueLogKey" in system
    assert "dialogueDigest" in system
    assert "subjectRelevance.shouldPersist 必须保守" in system
    assert "不得创建 scoped memory" in system
    assert "不得输出 memoryPatch" in system
    assert "memoryPatch 只能写短摘要" not in system


def test_provider_normalization_forces_memory_patch_null_and_cleans_dialogue_digest() -> None:
    payload = _normalize_provider_response_payload({
        "answer": "收到",
        "dialogueDigest": {
            "coreIntent": "看看这套房源怎么谈",
            "discussionSummary": "先判断业主底线。",
            "finalOutcome": "下一步先约业主复盘带看反馈。",
            "userAcceptance": "采纳",
            "subjectRelevance": {"level": "direct", "shouldPersist": True, "reason": "围绕当前房源维护。"},
        },
        "memoryPatch": {"businessFacts": ["不该写"]},
        "toolAudit": [{"tool": "provider.chat_completions", "action": "x", "status": "ok"}],
    })
    response = SuFenResponse.model_validate(payload)
    assert response.memoryPatch is None
    assert response.dialogueDigest is not None
    assert response.dialogueDigest.userAcceptance == "accepted"
    assert response.dialogueDigest.subjectRelevance.shouldPersist is True


def test_provider_tool_args_strip_memory_scope() -> None:
    task = make_task()
    clean = _task_bound_tool_args("sufen_memory_search", {
        "query": "业主",
        "companyId": "evil",
        "operatorUserId": "evil",
        "subjectType": "broker",
        "subjectId": "B-1",
        "scope": {"subjectId": "B-1"},
        "memoryRoot": "/tmp/evil",
        "admin": True,
    }, task)
    assert clean == {"query": "业主"}


def test_provider_payload_exposes_only_current_sufen_tools() -> None:
    payload = build_provider_payload("怎么聊", make_task(), settings=type("Settings", (), {"model": "test"})())
    tool_names = {tool["function"]["name"] for tool in payload["tools"]}
    assert "sufen_memory_search" in tool_names
    assert "sufen_memory_patch_draft" not in tool_names
    assert payload["response_format"] == {"type": "json_object"}


def test_property_strategy_no_long_term_memory_patch() -> None:
    task = make_task()
    response = build_property_archive_response(prompt="AUTH-P-1 这个业主怎么聊", refs=[], task=task, initial_audit=[])
    assert response.memoryPatch is None
    assert any(item.tool == "sufen_memory_search" and item.action == "read_human_memory_root" for item in response.toolAudit)
    assert not any(item.tool == "sufen_memory_patch_draft" for item in response.toolAudit)


def test_terminal_input_is_only_fallback(monkeypatch) -> None:
    class Settings:
        model = "deepseek-v4-pro"
        provider = "deepseek"
        base_url = ""

    monkeypatch.setattr("sufen.terminal_ui._can_use_prompt_toolkit", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt: "你好")
    session = TerminalPromptSession(Settings())
    assert session.prompt() == "你好"
