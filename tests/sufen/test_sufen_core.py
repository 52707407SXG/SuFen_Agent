from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from sufen.memory import iter_memory_documents, memory_root, search_human_memory
from sufen.output import SuFenResponse
from sufen.property_strategy import build_property_archive_response
from sufen.provider import (
    _authorized_context_fallback_response,
    _classify_user_intent,
    _compact_sparse_property_answer_if_needed,
    _extract_json_object,
    _final_answer_guardrail,
    _normalize_provider_response_payload,
    _prefix_property_owner_boundary_if_needed,
    _system_message,
    _task_bound_tool_args,
    build_provider_payload,
    provider_fail_closed_response,
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
    assert "汤总" in system
    assert "某总/某姐/某哥" in system
    assert "公开资料显示/我查到" in system
    assert "不得因为用户随口问了一句就输出全维度分析" in system
    assert "用户本轮原话是最高优先级的意图来源" in system
    assert "寒暄和简单确认不得因为当前页面有档案或图谱缺失" in system
    assert "不得输出“低置信度”口头禅" in system


def test_authorized_context_fallback_uses_verified_facts_only() -> None:
    task = make_task(archiveContext={
        "companyId": "company-ZYJ",
        "authorizationId": "AUTH-P-1",
        "archive": {
            "id": "P-1",
            "type": "property",
            "displayName": "虚拟花园 1-1-101",
            "ownerName": "汤总",
            "grade": "A",
            "score": 88,
            "status": "谈判中",
            "summary": "高优先级房源，业主很强势。",
            "fields": {"楼盘": "虚拟花园", "房号": "1-1-101", "面积": "185平"},
            "verifiedFacts": {
                "fields": {"楼盘": "虚拟花园", "房号": "1-1-101", "面积": "185平"},
                "events": [],
            },
            "sourceQuality": {
                "score": {
                    "status": "legacy_unverified_score",
                    "value": 88,
                    "usableAsCurrentFact": False,
                    "reason": "只有旧顶层 score。",
                },
                "evidenceCompleteness": {
                    "status": "current_facts_sparse",
                    "reason": "当前可见字段偏少。",
                },
            },
        },
        "dialogueLogBrief": [],
        "requiredKnowledgeGraph": {
            "requiredName": "房源维护",
            "status": "configured_placeholder",
            "refId": "KGREF-property-maintenance",
        },
        "knowledgeGraphBinding": {
            "requiredName": "房源维护",
            "status": "configured_placeholder",
            "requiredRefId": "KGREF-property-maintenance",
        },
    })
    response = _authorized_context_fallback_response(
        prompt="上一轮输出错误地要求用户补站内ID。\n这套房子怎么样？",
        previous_response=SuFenResponse(answer="缺关键资料"),
        task=task,
        loop_audit=[],
    )
    assert "虚拟花园" in response.answer
    assert "185平" in response.answer
    assert "谈判中" not in response.answer
    assert "高优先级" not in response.answer
    assert "很强势" not in response.answer
    assert "88" not in response.answer
    assert "汤总" not in response.answer
    assert "上一轮输出错误" not in response.answer
    assert "站内ID" not in response.answer
    assert "评分未校验" in response.answer
    assert "现在不要硬下结论" in response.answer
    assert response.evidenceUsed[0].confidence <= 0.6
    assert response.dialogueDigest is not None
    assert response.dialogueDigest.subjectRelevance.shouldPersist is False


def test_sparse_property_broad_question_is_compacted() -> None:
    task = make_task(archiveContext={
        "companyId": "company-ZYJ",
        "archive": {
            "id": "P-1",
            "type": "property",
            "displayName": "虚拟花园 1-1-101",
            "fields": {"楼盘": "虚拟花园", "房号": "1-1-101", "面积": "185平", "户型": "四室两厅", "报价": "880万"},
            "verifiedFacts": {
                "fields": {"楼盘": "虚拟花园", "房号": "1-1-101", "面积": "185平", "户型": "四室两厅", "报价": "880万"},
                "events": [],
            },
            "sourceQuality": {
                "evidenceCompleteness": {"status": "current_facts_sparse"},
            },
        },
    })
    response = SuFenResponse(
        answer="\n".join([
            "已知事实",
            "1. 楼盘虚拟花园",
            "2. 面积185平",
            "3. 报价880万",
            "4. 缺知识图谱",
            "5. 缺评分卡",
            "6. 缺公开行情",
            "7. 缺业主特征卡",
            "8. 缺带看反馈",
        ]) * 20,
        dialogueDigest={
            "coreIntent": "判断这套房子怎么样",
            "discussionSummary": "模型输出过长。",
            "finalOutcome": "未形成结论。",
            "userAcceptance": "unclear",
            "subjectRelevance": {"level": "direct", "shouldPersist": True, "reason": "模型误判。"},
        },
    )
    compact = _compact_sparse_property_answer_if_needed(response, task=task, prompt="这套房子怎么样？")
    assert len(compact.answer) < 320
    assert "不能给分数、成交概率或完整结论" in compact.answer
    assert "低置信度" not in compact.answer
    assert "provider.postprocess" in {item.tool for item in compact.toolAudit}
    assert compact.dialogueDigest is not None
    assert compact.dialogueDigest.subjectRelevance.shouldPersist is False


def test_sparse_property_medium_answer_with_owner_or_score_is_compacted() -> None:
    task = make_task(archiveContext={
        "companyId": "company-ZYJ",
        "archive": {
            "id": "P-1",
            "type": "property",
            "displayName": "虚拟花园 1-1-101",
            "fields": {"楼盘": "虚拟花园", "房号": "1-1-101", "面积": "185平", "报价": "880万"},
            "verifiedFacts": {
                "fields": {"楼盘": "虚拟花园", "房号": "1-1-101", "面积": "185平", "报价": "880万"},
                "events": [],
            },
            "sourceQuality": {
                "evidenceCompleteness": {"status": "current_facts_sparse"},
            },
        },
    })
    response = SuFenResponse(
        answer="刚哥，这套房还看不清。五维评分卡里价格偏低，业主“汤总”的称呼不能推性格。现在还缺房源笔记、公开行情和带看反馈，要先补材料再判断。",
        dialogueDigest={
            "coreIntent": "判断这套房子怎么样",
            "discussionSummary": "展开了评分卡和业主称呼。",
            "finalOutcome": "未形成结论。",
            "userAcceptance": "unclear",
            "subjectRelevance": {"level": "direct", "shouldPersist": True, "reason": "模型误判。"},
        },
    )
    compact = _compact_sparse_property_answer_if_needed(response, task=task, prompt="这套房子怎么样？")
    assert "五维评分" not in compact.answer
    assert "汤总" not in compact.answer
    assert len(compact.answer) < 320
    assert compact.dialogueDigest is not None
    assert compact.dialogueDigest.subjectRelevance.shouldPersist is False


def test_owner_communication_gets_evidence_boundary_prefix() -> None:
    task = make_task(archiveContext={
        "companyId": "company-ZYJ",
        "archive": {
            "id": "P-1",
            "type": "property",
            "fields": {"业主姓名": "汤总"},
            "verifiedFacts": {"fields": {"业主姓名": "汤总"}, "events": [{"id": "evt-1", "title": "上周未接电话"}]},
            "sourceQuality": {
                "subjectFeatureCard": {"status": "not_loaded"},
                "evidenceCompleteness": {"status": "current_facts_sparse"},
            },
        },
    })
    response = SuFenResponse(
        answer="开口可以先问汤总什么时候方便聊房子。",
        dialogueDigest={
            "coreIntent": "问业主怎么沟通",
            "discussionSummary": "给了话术。",
            "finalOutcome": "建议先联系。",
            "userAcceptance": "unclear",
            "subjectRelevance": {"level": "direct", "shouldPersist": True, "reason": "围绕业主沟通。"},
        },
    )
    patched = _prefix_property_owner_boundary_if_needed(response, task=task, prompt="这个业主怎么开口比较好？")
    assert patched.answer.startswith("我先不判断业主心态")
    assert "别急着猜他的价格心理" in patched.answer
    assert "目前只能低置信度处理" not in patched.answer
    assert "provider.postprocess" in {item.tool for item in patched.toolAudit}


def test_property_postprocessors_use_raw_user_message_not_background_prompt() -> None:
    task = make_task(archiveContext={
        "companyId": "company-ZYJ",
        "userMessageForSufen": "随便聊聊",
        "sufenUserIntent": "casual_chat",
        "archive": {
            "id": "P-1",
            "type": "property",
            "fields": {"业主姓名": "汤总", "楼盘": "虚拟花园"},
            "verifiedFacts": {"fields": {"业主姓名": "汤总", "楼盘": "虚拟花园"}, "events": []},
            "sourceQuality": {
                "subjectFeatureCard": {"status": "not_loaded"},
                "evidenceCompleteness": {"status": "current_facts_sparse"},
            },
        },
    })
    prompt = "用户本轮原话（最高优先用于意图判断）：\n随便聊聊\n\n后台档案事实包：业主怎么沟通；这套房子怎么样。"
    response = SuFenResponse(
        answer="刚哥，今晚想聊点什么？",
        dialogueDigest={
            "coreIntent": "闲聊",
            "discussionSummary": "简短回应。",
            "finalOutcome": "未形成业务结论。",
            "userAcceptance": "chat",
            "subjectRelevance": {"level": "none", "shouldPersist": False, "reason": "闲聊。"},
        },
    )
    compact = _compact_sparse_property_answer_if_needed(response, task=task, prompt=prompt)
    patched = _prefix_property_owner_boundary_if_needed(compact, task=task, prompt=prompt)
    assert patched.answer == "刚哥，今晚想聊点什么？"
    assert not any(item.action in {"compact_sparse_property_answer", "prefix_property_owner_boundary"} for item in patched.toolAudit)


def test_casual_greeting_is_hidden_from_archive_context_and_short() -> None:
    task = make_task(
        operator={"userId": "52707407", "name": "刚哥", "role": "admin", "isGangGe": True},
        archiveContext={
            "companyId": "company-ZYJ",
            "archive": {
                "id": "M000001:FYWH1",
                "type": "property",
                "displayName": "中海城南一号 2-1-1001",
                "ownerName": "汤永明",
                "fields": {"楼盘": "中海城南一号", "房号": "2-1-1001", "业主姓名": "汤永明"},
                "verifiedFacts": {"fields": {"楼盘": "中海城南一号", "房号": "2-1-1001", "业主姓名": "汤永明"}, "events": []},
                "sourceQuality": {"evidenceCompleteness": {"status": "current_facts_sparse"}},
            },
            "requiredKnowledgeGraph": {
                "requiredName": "房源维护",
                "status": "configured_placeholder",
                "refId": "KGREF-property-maintenance",
            },
            "userMessageForSufen": "sufen晚上好",
            "sufenUserIntent": "casual_greeting",
        },
        requiredKnowledgeGraph={"requiredName": "房源维护", "status": "configured_placeholder", "refId": "KGREF-property-maintenance"},
    )
    response = SuFenResponse(
        answer="目前只能低置信度看：中海城南一号 2-1-1001，业主汤永明，资料不足。",
        dialogueDigest={
            "coreIntent": "寒暄",
            "discussionSummary": "错误展开了当前房源。",
            "finalOutcome": "错误输出。",
            "userAcceptance": "unclear",
            "subjectRelevance": {"level": "direct", "shouldPersist": True, "reason": "错误。"},
        },
        evidenceUsed=[{"source": "archive", "summary": "中海城南一号 2-1-1001", "confidence": 0.5}],
    )
    patched = _final_answer_guardrail(response, task=task, prompt="sufen晚上好")
    assert patched.answer == "刚哥晚上好，我在。你先说，我听着。"
    assert "低置信度" not in patched.answer
    assert "中海城南一号" not in patched.answer
    assert "汤永明" not in patched.answer
    assert patched.evidenceUsed == []
    assert patched.dialogueDigest is not None
    assert patched.dialogueDigest.userAcceptance == "chat"
    assert patched.dialogueDigest.subjectRelevance.shouldPersist is False
    assert any(item.tool == "provider.output_guardrail" and item.action == "casual_short_answer" for item in patched.toolAudit)


def test_casual_chat_leaked_archive_context_is_cleaned() -> None:
    task = make_task(
        operator={"userId": "52707407", "name": "刚哥", "role": "admin", "isGangGe": True},
        archiveContext={
            "companyId": "company-ZYJ",
            "archive": {
                "id": "M000001:FYWH1",
                "type": "property",
                "displayName": "中海城南一号 2-1-1001",
                "ownerName": "汤永明",
                "fields": {"楼盘": "中海城南一号", "房号": "2-1-1001", "业主姓名": "汤永明"},
            },
            "userMessageForSufen": "随便聊聊",
            "sufenUserIntent": "casual_chat",
        },
    )
    response = SuFenResponse(
        answer="中海城南一号 2-1-1001 的业主汤永明当前资料缺口较多。",
        dialogueDigest={
            "coreIntent": "闲聊",
            "discussionSummary": "错误泄露档案背景。",
            "finalOutcome": "错误输出。",
            "userAcceptance": "unclear",
            "subjectRelevance": {"level": "direct", "shouldPersist": True, "reason": "错误。"},
        },
        evidenceUsed=[{"source": "archive", "summary": "中海城南一号 2-1-1001", "confidence": 0.5}],
    )
    patched = _final_answer_guardrail(response, task=task, prompt="随便聊聊")
    assert patched.answer == "刚哥，我在。你先说，我听着。"
    assert "中海城南一号" not in patched.answer
    assert "汤永明" not in patched.answer
    assert patched.evidenceUsed == []
    assert patched.dialogueDigest is not None
    assert patched.dialogueDigest.subjectRelevance.shouldPersist is False
    assert any(item.tool == "provider.output_guardrail" and item.action == "casual_chat_background_cleanup" for item in patched.toolAudit)


def test_final_guardrail_naturalizes_fixed_low_confidence_language() -> None:
    task = make_task(archiveContext={"userMessageForSufen": "这套房子怎么样？"})
    response = SuFenResponse(
        answer="目前只能低置信度处理：房源笔记和公开行情都没核实，只能低置信度回答。",
        dialogueDigest={
            "coreIntent": "判断房源",
            "discussionSummary": "说明资料不足。",
            "finalOutcome": "未形成结论。",
            "userAcceptance": "unclear",
            "subjectRelevance": {"level": "direct", "shouldPersist": False, "reason": "资料不足。"},
        },
    )
    patched = _final_answer_guardrail(response, task=task, prompt="这套房子怎么样？")
    assert "低置信度" not in patched.answer
    assert patched.answer.startswith("资料还薄，我先收住判断")
    assert "只能先克制回答" in patched.answer
    assert any(item.tool == "provider.output_guardrail" and item.action == "naturalize_public_boundary_language" for item in patched.toolAudit)


def test_intent_classifier_keeps_business_questions_out_of_greeting_fast_path() -> None:
    task = make_task()
    assert _classify_user_intent("sufen晚上好", task) == "casual_greeting"
    assert _classify_user_intent("晚上好，这套房子怎么样？", task) == "strategy_question"
    assert _classify_user_intent("这个业主怎么聊？", task) == "owner_communication"


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


def test_provider_fail_closed_keeps_dialogue_digest_contract() -> None:
    response = provider_fail_closed_response("tool_loop_exceeded", "max_turns:4")
    assert response.dialogueDigest is not None
    assert response.dialogueDigest.subjectRelevance.shouldPersist is False
    assert "未形成可入档的业务结论" in response.dialogueDigest.finalOutcome


def test_provider_json_extraction_tolerates_control_characters() -> None:
    payload = _extract_json_object('{"answer":"第一行\n第二行","memoryPatch":null}')
    assert payload["answer"] == "第一行\n第二行"


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
