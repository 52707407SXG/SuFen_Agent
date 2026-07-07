from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from agent.system_prompt import build_system_prompt, build_system_prompt_parts
from agent.transports.chat_completions import ChatCompletionsTransport
from sufen.auth import FAIL_CLOSED_MESSAGE, extract_authorization_refs
from sufen.chat import answer_sufen
import sufen.config as sufen_config
from sufen.config import load_settings
from sufen.fake_provider import answer_with_fake_provider
from sufen.memory import draft_memory_patch, memory_path
from sufen.output import SuFenResponse
from sufen.server import create_app
from sufen.session import SuFenSession
from sufen.task_package import (
    AgentDelegationToken,
    SuFenTaskPackage,
    clear_delegation_nonce_cache,
    ensure_safe_actions,
    sign_delegation_token,
)
from toolsets import SUFEN_TOOL_NAMES, get_toolset_names, resolve_toolset, validate_toolset
from tools.registry import registry
import tools.sufen_mystand_tools  # noqa: F401


REPO_ROOT = Path(__file__).resolve().parents[2]
LEGACY_BRAND = "Her" + "mes"
LEGACY_LOWER = LEGACY_BRAND.lower()
DELEGATION_SECRET = "unit-test-" + "delegation-secret"


def _property_task(**updates):
    data = {
        "operator": {"userId": "1001", "name": "经纪人A", "role": "broker"},
        "subject": {"type": "property", "id": "P-1"},
        "scene": "房源维护",
        "archiveContext": {
            "companyId": "company-ZYJ",
            "authorizationId": "AUTH-P-1",
            "baseInfo": {"title": "阳光花园三居", "askingPrice": "480万"},
            "propertyNote": "业主说先按原价挂一周。",
            "eventSummary": ["近三天有两组看房但无明确报价"],
            "knowledgeGraphs": {
                "KGREF-property-maintenance": {
                    "name": "房源维护知识图谱",
                    "focus": "业主维护、价格弹性、带看反馈",
                }
            },
        },
        "brokerProfile": {"capabilityStage": "新手"},
        "knowledgeGraphRefs": ["KGREF-property-maintenance"],
        "scopedMemoryKey": "company-ZYJ/operators/1001/subjects/property/P-1",
    }
    data.update(updates)
    return data


def _signed_delegation_token(
    *,
    subject: dict | None = None,
    operator_user_id: str = "1001",
    allowed_actions: list[str] | None = None,
    expires_at: datetime | None = None,
    nonce: str = "nonce-1",
    secret: str = DELEGATION_SECRET,
) -> AgentDelegationToken:
    token = AgentDelegationToken.model_validate({
        "actorAgent": "lucan",
        "operatorUserId": operator_user_id,
        "subject": subject or {"type": "property", "id": "P-1"},
        "allowedActions": allowed_actions or [
            "analyze",
            "suggest",
            "eventDraft",
            "fieldPatchDraft",
            "memoryPatch",
        ],
        "expiresAt": (expires_at or (datetime.now(timezone.utc) + timedelta(hours=1))).isoformat(),
        "nonce": nonce,
        "signature": "pending",
    })
    return token.model_copy(update={"signature": sign_delegation_token(token, secret)})


def test_sufen_env_does_not_reuse_miner_key(monkeypatch):
    monkeypatch.delenv("SUFEN_API_KEY", raising=False)
    monkeypatch.delenv("SUFEN_SERVICE_API_KEY", raising=False)
    monkeypatch.delenv("SUFEN_PROVIDER_API_KEY", raising=False)
    monkeypatch.setenv("MYSTAND_MINER_API_KEY", "miner-secret")
    monkeypatch.setattr(sufen_config, "_candidate_env_files", lambda: [])
    settings = load_settings()
    assert settings.api_key == ""
    assert settings.service_api_key == ""
    assert settings.provider_api_key == ""


def test_sufen_loads_local_dotenv_and_keeps_process_env_priority(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SUFEN_API_KEY", raising=False)
    monkeypatch.delenv("SUFEN_SERVICE_API_KEY", raising=False)
    monkeypatch.delenv("SUFEN_PROVIDER_API_KEY", raising=False)
    monkeypatch.delenv("SUFEN_PORT", raising=False)
    monkeypatch.setattr(sufen_config, "_candidate_env_files", lambda: [tmp_path / ".env"])
    (tmp_path / ".env").write_text(
        "\n".join([
            "SUFEN_SERVICE_API_KEY=service-dotenv",
            "SUFEN_PROVIDER_API_KEY=provider-dotenv",
            "SUFEN_API_KEY=deprecated-dotenv",
            "SUFEN_PORT=8799",
            "MYSTAND_MINER_API_KEY=must-not-load",
        ]),
        encoding="utf-8",
    )

    settings = load_settings()
    assert settings.service_api_key == "service-dotenv"
    assert settings.provider_api_key == "provider-dotenv"
    assert settings.api_key == "deprecated-dotenv"
    assert settings.port == 8799

    monkeypatch.setenv("SUFEN_PROVIDER_API_KEY", "provider-process")
    assert load_settings().provider_api_key == "provider-process"


def test_deprecated_sufen_api_key_fallback_is_explicit(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SUFEN_SERVICE_API_KEY", raising=False)
    monkeypatch.delenv("SUFEN_PROVIDER_API_KEY", raising=False)
    monkeypatch.setenv("SUFEN_API_KEY", "fallback-key")
    monkeypatch.setattr(sufen_config, "_candidate_env_files", lambda: [])
    settings = load_settings()
    assert settings.api_key == "fallback-key"
    assert settings.service_api_key == "fallback-key"
    assert settings.provider_api_key == "fallback-key"


def test_authorization_refs_and_fail_closed():
    refs = extract_authorization_refs("看 AUTH-P123 和 KGREF-house-maintenance 以及 knowledge:owner")
    assert [ref.raw for ref in refs] == ["AUTH-P123", "KGREF-house-maintenance", "knowledge:owner"]
    response = answer_with_fake_provider("这个业主该怎么聊")
    assert response.missingAuthorizationRequests
    assert response.answer == FAIL_CLOSED_MESSAGE


def test_scoped_memory_path_isolated_and_ascii_only(tmp_path):
    first = memory_path(
        company_id="company-ZYJ",
        operator_user_id="1001",
        subject_type="property",
        subject_id="P-1",
        root=tmp_path,
    )
    second = memory_path(
        company_id="company-ZYJ",
        operator_user_id="1002",
        subject_type="property",
        subject_id="P-1",
        root=tmp_path,
    )
    assert first != second
    assert "operators/1001/subjects/property/P-1/memory.json" in first.as_posix()
    try:
        memory_path(
            company_id="company-ZYJ",
            operator_user_id="张三",
            subject_type="property",
            subject_id="P-1",
            root=tmp_path,
        )
    except ValueError as exc:
        assert "stable ASCII" in str(exc)
    else:
        raise AssertionError("Chinese display names must not become path segments")


def test_memory_patch_is_draft_only():
    patch = draft_memory_patch(
        {"companyId": "company-ZYJ", "operatorUserId": "1001", "subjectType": "property", "subjectId": "P-1"},
        {"businessFacts": ["业主明确说过先不降价"], "ignored": "nope"},
    )
    assert patch["draftOnly"] is True
    assert "ignored" not in patch["patch"]


def test_sufen_session_transcript_isolated(tmp_path):
    session = SuFenSession("operator-1001:property:P-1", root=tmp_path)
    session.append_turn(role="user", content="AUTH-P-1 帮我判断")
    session.append_turn(role="assistant", content={"answer": "草稿"}, metadata={"draftOnly": True})
    rows = session.read_transcript()
    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert rows[1]["metadata"]["draftOnly"] is True

    try:
        SuFenSession("../escape", root=tmp_path).append_turn(role="user", content="bad")
    except ValueError as exc:
        assert "stable ASCII" in str(exc)
    else:
        raise AssertionError("unsafe session id must fail closed")


def test_tool_whitelist_and_draft_tools():
    assert set(resolve_toolset("sufen")) == set(SUFEN_TOOL_NAMES)
    assert len(resolve_toolset("sufen")) == len(SUFEN_TOOL_NAMES)

    event = registry.dispatch("mystand.event.draft", {"name": "回访业主", "body": "确认挂牌底线"})
    assert event["eventDraft"]["draftOnly"] is True

    diff = registry.dispatch("mystand.field_patch_draft", {"field": "priceNote", "before": "急售", "after": "可谈"})
    assert diff["fieldPatchDraft"]["draftOnly"] is True
    assert "-急售" in diff["fieldPatchDraft"]["diff"]
    assert "+可谈" in diff["fieldPatchDraft"]["diff"]


def test_sufen_memory_search_scope_rejects_model_selected_root_and_admin(monkeypatch):
    monkeypatch.setenv("SUFEN_AGENT_MODE", "1")
    monkeypatch.setenv("SUFEN_MEMORY_ROOT", "/var/lib/sufen-agent/memory")

    schema = registry.get_schema("sufen_memory_search")
    properties = schema["parameters"]["properties"]
    assert set(properties) == {"query"}
    assert "companyId" not in properties
    assert "operatorUserId" not in properties
    assert "subjectType" not in properties
    assert "subjectId" not in properties
    assert "memoryRoot" not in properties
    assert "admin" not in properties

    task = SuFenTaskPackage.model_validate(_property_task())
    result = registry.dispatch("sufen_memory_search", {
        "query": "底价",
        "memoryRoot": "/tmp/model-selected-root",
        "admin": True,
    }, task_package=task)
    assert "/tmp/model-selected-root" not in result["path"]
    assert "/admin/" not in result["path"]
    assert "/operators/1001/subjects/property/P-1/memory.json" in result["path"]

    cross_scope = registry.dispatch("sufen_memory_search", {
        "query": "底价",
        "operatorUserId": "1002",
        "subjectId": "P-OTHER",
    }, task_package=task)
    assert cross_scope["ok"] is False
    assert "memory_scope_mismatch" in cross_scope["reason"]


def test_sufen_task_bound_tool_schemas_hide_authority_fields():
    archive_props = registry.get_schema("mystand.archive.read")["parameters"]["properties"]
    kg_props = registry.get_schema("mystand.knowledge_graph.read")["parameters"]["properties"]
    memory_props = registry.get_schema("sufen_memory_search")["parameters"]["properties"]
    memory_patch_props = registry.get_schema("sufen_memory_patch_draft")["parameters"]["properties"]

    assert set(archive_props) == {"authorizationId"}
    assert set(kg_props) == {"knowledgeGraphRef"}
    assert set(memory_props) == {"query"}
    assert set(memory_patch_props) == {"patch"}

    hidden = {
        "authorizedPayload",
        "companyId",
        "operatorUserId",
        "subjectType",
        "subjectId",
        "archiveContext",
        "knowledgeGraphRefs",
        "scopedMemoryKey",
        "memoryRoot",
        "admin",
        "scope",
    }
    for props in (archive_props, kg_props, memory_props, memory_patch_props):
        assert not hidden.intersection(props)


def test_sufen_web_tools_use_sufen_tavily_key(monkeypatch):
    monkeypatch.setenv("SUFEN_AGENT_MODE", "1")
    monkeypatch.setenv("SUFEN_TAVILY_API_KEY", "sufen-tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "generic-tavily")

    from tools.registry import invalidate_check_fn_cache
    import tools.web_tools as web_tools

    invalidate_check_fn_cache()
    assert web_tools._env_value("TAVILY_API_KEY") == "sufen-tavily"
    assert web_tools._has_env("TAVILY_API_KEY") is True
    assert web_tools._get_backend() == "tavily"
    assert web_tools._is_backend_available("tavily") is True
    assert web_tools.check_web_api_key() is True
    assert web_tools._web_requires_env() == ["SUFEN_TAVILY_API_KEY"]


def test_sufen_web_tools_use_dotenv_tavily_key(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUFEN_AGENT_MODE", "1")
    monkeypatch.delenv("SUFEN_TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    (tmp_path / ".env").write_text("SUFEN_TAVILY_API_KEY=sufen-dotenv-tavily\n", encoding="utf-8")

    from tools.registry import invalidate_check_fn_cache
    import tools.web_tools as web_tools

    invalidate_check_fn_cache()
    assert web_tools._env_value("TAVILY_API_KEY") == "sufen-dotenv-tavily"
    assert web_tools._is_backend_available("tavily") is True
    assert web_tools.check_web_api_key() is True


def test_sufen_web_tools_ignore_generic_tavily_in_sufen_mode(monkeypatch):
    monkeypatch.setenv("SUFEN_AGENT_MODE", "1")
    monkeypatch.delenv("SUFEN_TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "generic-tavily")

    from tools.registry import invalidate_check_fn_cache
    import tools.web_tools as web_tools

    invalidate_check_fn_cache()
    assert web_tools._env_value("TAVILY_API_KEY") == ""
    assert web_tools._has_env("TAVILY_API_KEY") is False
    assert web_tools._is_backend_available("tavily") is False
    assert web_tools.check_web_api_key() is False


def test_sufen_tavily_provider_reads_sufen_key(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUFEN_AGENT_MODE", "1")
    monkeypatch.delenv("SUFEN_TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    (tmp_path / ".env").write_text("SUFEN_TAVILY_API_KEY=sufen-dotenv-tavily\n", encoding="utf-8")

    from plugins.web.tavily.provider import TavilyWebSearchProvider, _tavily_api_key

    assert _tavily_api_key() == "sufen-dotenv-tavily"
    assert TavilyWebSearchProvider().is_available() is True

    monkeypatch.setenv("TAVILY_API_KEY", "generic-tavily")
    assert _tavily_api_key() == "sufen-dotenv-tavily"


def test_sufen_tool_definitions_are_exact_whitelist_with_sufen_tavily(monkeypatch):
    monkeypatch.setenv("SUFEN_AGENT_MODE", "1")
    monkeypatch.setenv("SUFEN_TAVILY_API_KEY", "sufen-tavily")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    from tools.registry import discover_builtin_tools, invalidate_check_fn_cache
    import model_tools
    import tools.web_tools  # noqa: F401

    discover_builtin_tools()
    invalidate_check_fn_cache()
    model_tools._clear_tool_defs_cache()

    definitions = model_tools.get_tool_definitions(
        enabled_toolsets=["sufen"],
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )
    names = {item["function"]["name"] for item in definitions}

    assert names == set(SUFEN_TOOL_NAMES)
    assert not {
        "terminal",
        "write_file",
        "patch",
        "execute_code",
        "browser_navigate",
        "discord",
        "slack",
        "telegram_send",
    } & names


def test_sufen_mode_closes_inherited_toolsets(monkeypatch):
    monkeypatch.setenv("SUFEN_AGENT_MODE", "1")
    monkeypatch.setenv("SUFEN_TAVILY_API_KEY", "sufen-tavily")

    assert get_toolset_names() == ["sufen"]
    assert set(resolve_toolset("all")) == set(SUFEN_TOOL_NAMES)
    assert resolve_toolset("web") == []
    assert resolve_toolset("terminal") == []
    assert resolve_toolset("sufen-cli") == []
    assert resolve_toolset("sufen-discord") == []
    assert validate_toolset("sufen") is True
    assert validate_toolset("all") is True
    assert validate_toolset("web") is False
    assert validate_toolset("sufen-cli") is False


def test_sufen_mode_default_tool_definitions_are_whitelist(monkeypatch):
    monkeypatch.setenv("SUFEN_AGENT_MODE", "1")
    monkeypatch.setenv("SUFEN_TAVILY_API_KEY", "sufen-tavily")

    from tools.registry import discover_builtin_tools, invalidate_check_fn_cache
    import model_tools
    import tools.web_tools  # noqa: F401

    discover_builtin_tools()
    invalidate_check_fn_cache()
    model_tools._clear_tool_defs_cache()

    definitions = model_tools.get_tool_definitions(
        enabled_toolsets=None,
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )
    names = {item["function"]["name"] for item in definitions}

    assert names == set(SUFEN_TOOL_NAMES)


def test_sufen_mode_builtin_tool_discovery_is_limited():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; "
                "from tools.registry import discover_builtin_tools; "
                "print(json.dumps(discover_builtin_tools()))"
            ),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
        env={
            **os.environ,
            "SUFEN_AGENT_MODE": "1",
            "SUFEN_TAVILY_API_KEY": "sufen-tavily",
        },
    )
    imported = json.loads(result.stdout.strip().splitlines()[-1])
    assert imported == ["tools.sufen_mystand_tools", "tools.web_tools"]
    assert "permanent allowlist" not in result.stdout
    assert "permanent allowlist" not in result.stderr


def test_sufen_mode_model_tools_import_registers_only_whitelist():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, model_tools; "
                "print(json.dumps(sorted(model_tools.TOOL_TO_TOOLSET_MAP)))"
            ),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
        env={
            **os.environ,
            "SUFEN_AGENT_MODE": "1",
            "SUFEN_TAVILY_API_KEY": "sufen-tavily",
        },
    )
    registered = set(json.loads(result.stdout.strip().splitlines()[-1]))
    assert registered == set(SUFEN_TOOL_NAMES)
    assert "permanent allowlist" not in result.stdout
    assert "permanent allowlist" not in result.stderr


def test_sufen_mode_provider_discovery_is_deepseek_only():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; "
                "from providers import list_providers; "
                "print(json.dumps(sorted(p.name for p in list_providers())))"
            ),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
        env={**os.environ, "SUFEN_AGENT_MODE": "1"},
    )
    assert json.loads(result.stdout.strip().splitlines()[-1]) == ["deepseek"]


class _RuntimeStub:
    @staticmethod
    def build_nous_subscription_prompt(_tools):
        return ""

    @staticmethod
    def build_environment_hints():
        return ""


class _LegacyRuntimeStub:
    @staticmethod
    def build_nous_subscription_prompt(_tools):
        return "SuFen Agent legacy block with sufen status and portal billing."

    @staticmethod
    def build_environment_hints():
        return ""

    @staticmethod
    def load_soul_md(_ctx_len):
        raise AssertionError("SuFen mode must not load inherited SOUL identity")


class _AgentStub:
    load_soul_identity = False
    skip_context_files = True
    context_compressor = None
    valid_tool_names = []
    provider = "fake"
    model = "fake-sufen"
    platform = "cli"
    _tool_use_enforcement = False
    _task_completion_guidance = False
    _parallel_tool_call_guidance = False
    _environment_probe = False
    _memory_store = None
    _memory_enabled = False
    _user_profile_enabled = False
    _memory_manager = None
    pass_session_id = False
    session_id = None

    def _emit_status(self, _message):
        return None


def test_sufen_policy_enters_actual_system_prompt(monkeypatch):
    import agent.system_prompt as system_prompt

    monkeypatch.setattr(system_prompt, "_ra", lambda: _RuntimeStub)
    parts = build_system_prompt_parts(_AgentStub())
    assert "你是 SuFen" in parts["stable"]
    assert "中文名：素分" in parts["stable"]
    assert "最高原则：会聊天的业务军师" in parts["stable"]
    assert "识别场景 -> 判断关系 -> 判断真实意图 -> 选择必要资料" in parts["stable"]
    assert "角色感、空间感、时间感" in parts["stable"]
    assert "操作者和档案对象不同时" in parts["stable"]
    assert "操作者和档案对象相同时" in parts["stable"]
    assert "按北京时间 `Asia/Shanghai`" in parts["stable"]
    assert "意图判断和对话节奏" in parts["stable"]
    assert "按需资料选择" in parts["stable"]
    assert "不默认全量扫描" in parts["stable"]
    assert "不得默认读取结算卡" in parts["stable"]
    assert "签二手" in parts["stable"]
    assert "出房源" in parts["stable"]
    assert "省心租" in parts["stable"]
    assert "资料优先" in parts["stable"]
    assert "分层加载顺序" in parts["stable"]
    assert "contextLoadPlan" in parts["stable"]
    assert "验收要求" in parts["stable"]
    assert "companyId + operatorUserId + subjectType + subjectId" in parts["stable"]
    assert "不重要内容一律视为垃圾" in parts["stable"]
    assert "原始聊天、长文、图片 OCR、语音转写、附件全文" in parts["stable"]
    assert "answer" in parts["stable"]
    assert "Markdown" in parts["stable"]
    assert "taskPackage.archiveContext.archive" in parts["stable"]
    assert "不得因为用户没有额外粘贴" in parts["stable"]


def test_sufen_policy_builds_without_inherited_runtime(monkeypatch):
    monkeypatch.setenv("SUFEN_AGENT_MODE", "1")
    parts = build_system_prompt_parts(_AgentStub())
    assert "你是 SuFen" in parts["stable"]
    assert "SuFen 是 My Stand 里的深层业务军师" in parts["stable"]
    assert "不是报表机" in parts["stable"]
    assert "不能连续审问" in parts["stable"]
    assert "不做话痨" in parts["stable"]
    assert "资料优先" in parts["stable"]
    assert "实际 LLM 请求的 system message" in parts["stable"]
    assert "Markdown" in parts["stable"]
    assert "标记为 `loaded`" in parts["stable"]


def test_provider_system_message_allows_markdown_inside_answer():
    import sufen.provider as provider

    task = SuFenTaskPackage.model_validate(_property_task())
    system_message = provider.build_provider_messages("讲一下维护策略", task)[0]["content"]
    assert "只返回 JSON" in system_message
    assert "answer 字段" in system_message
    assert "Markdown" in system_message
    assert "代码围栏" in system_message
    assert "taskPackage.archiveContext.archive" in system_message
    assert "不得因为用户没有额外粘贴" in system_message
    assert "本轮 SuFen 执行锚点" in system_message
    assert "currentSufenTime" in system_message
    assert "Asia/Shanghai" in system_message
    assert "回答前先识别操作者" in system_message
    assert "不得默认全量扫描" in system_message
    assert "不得默认读取结算卡" in system_message
    assert "不做话痨" in system_message
    assert "contextLoadPlan" in system_message
    assert "未标记 loaded 的资料不得假装已读" in system_message


def test_sufen_time_defaults_to_beijing(monkeypatch):
    import sufen.time as sufen_time

    monkeypatch.delenv("SUFEN_TIMEZONE", raising=False)
    sufen_time.reset_cache()
    current = sufen_time.now()
    assert str(current.tzinfo) == "Asia/Shanghai"


def test_sufen_policy_reaches_chat_completion_request_system_message(monkeypatch):
    import agent.system_prompt as system_prompt

    monkeypatch.setattr(system_prompt, "_ra", lambda: _RuntimeStub)
    system_message = build_system_prompt(_AgentStub())
    transport = ChatCompletionsTransport()
    kwargs = transport.build_kwargs(
        model="deepseek-v4-pro",
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": "AUTH-P-1 帮我判断"},
        ],
        tools=None,
        model_lower="deepseek-v4-pro",
    )
    assert kwargs["messages"][0]["role"] == "system"
    assert "你是 SuFen" in kwargs["messages"][0]["content"]
    assert "资料优先" in kwargs["messages"][0]["content"]
    assert "禁止串线" in kwargs["messages"][0]["content"]
    assert "个人业务档案.md" in kwargs["messages"][0]["content"]


def test_sufen_system_prompt_skips_inherited_soul_and_subscription(monkeypatch):
    import agent.system_prompt as system_prompt

    monkeypatch.setenv("SUFEN_AGENT_MODE", "1")
    monkeypatch.setattr(system_prompt, "_ra", lambda: _LegacyRuntimeStub)
    agent = _AgentStub()
    agent.load_soul_identity = True
    agent.valid_tool_names = list(SUFEN_TOOL_NAMES)
    system_message = build_system_prompt(agent)

    forbidden = [LEGACY_BRAND, LEGACY_LOWER, "portal billing", "OpenClaw", "Xiaoban"]
    assert "你是 SuFen" in system_message
    for needle in forbidden:
        assert needle not in system_message


def test_sufen_packaged_core_does_not_import_inherited_time_module():
    critical_paths = [
        REPO_ROOT / "agent/system_prompt.py",
        REPO_ROOT / "agent/context_compressor.py",
    ]
    for path in critical_paths:
        assert "sufen_time" not in path.read_text(encoding="utf-8")


def test_output_schema_and_fake_provider_with_task_package():
    task = SuFenTaskPackage.model_validate({
        "operator": {"userId": "1001", "name": "经纪人A", "role": "broker"},
        "subject": {"type": "property", "id": "P-1"},
        "scene": "房源维护",
        "knowledgeGraphRefs": ["KGREF-house-maintenance"],
    })
    response = answer_with_fake_provider("AUTH-P-1 这个业主怎么判断", task=task)
    payload = SuFenResponse.model_validate(response.model_dump())
    assert payload.answer
    assert payload.evidenceUsed
    assert payload.toolAudit


def test_production_chat_uses_real_provider_not_fake(monkeypatch):
    clear_delegation_nonce_cache()
    monkeypatch.setenv("SUFEN_PROVIDER", "deepseek")
    monkeypatch.setenv("SUFEN_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("SUFEN_SERVICE_API_KEY", "service-key")
    monkeypatch.setenv("SUFEN_PROVIDER_API_KEY", "provider-key")
    monkeypatch.setenv("SUFEN_DELEGATION_HMAC_SECRET", DELEGATION_SECRET)
    monkeypatch.setenv("SUFEN_BASE_URL", "https://provider.test/v1")
    monkeypatch.setenv("SUFEN_FAKE_PROVIDER", "0")
    monkeypatch.setenv("SUFEN_TAVILY_API_KEY", "sufen-tavily")

    import sufen.fake_provider as fake_provider
    import sufen.provider as provider

    def fail_if_fake(*_args, **_kwargs):
        raise AssertionError("production path must not call fake_provider")

    captured = {}

    def fake_post(url, headers, payload):
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = payload
        content = {
            "answer": "real provider answer",
            "evidenceUsed": [],
            "missingAuthorizationRequests": [],
            "eventDrafts": [],
            "fieldPatchDrafts": [],
            "memoryPatch": None,
            "toolAudit": [{"tool": "provider.stub", "action": "respond", "status": "ok", "draftOnly": True}],
        }
        return {"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]}

    monkeypatch.setattr(fake_provider, "answer_with_fake_provider", fail_if_fake)
    monkeypatch.setattr(provider, "_post_chat_completions", fake_post)

    delegation = _signed_delegation_token(nonce="nonce-provider-content")
    task = SuFenTaskPackage.model_validate(_property_task(delegationToken=delegation.model_dump()))
    response = answer_sufen("AUTH-P-1 KGREF-property-maintenance", task=task, settings=load_settings())

    assert response.answer == "real provider answer"
    assert captured["url"] == "https://provider.test/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer provider-key"
    assert "你是 SuFen" in captured["payload"]["messages"][0]["content"]
    assert "scoped memory" in captured["payload"]["messages"][0]["content"]
    exposed = {item["function"]["name"] for item in captured["payload"]["tools"]}
    assert exposed == {provider.provider_tool_name(name) for name in SUFEN_TOOL_NAMES}
    assert all("." not in name for name in exposed)
    assert "mystand_archive_read" in captured["payload"]["messages"][0]["content"]
    assert any(item.tool == "provider.chat_completions" for item in response.toolAudit)


def test_provider_system_message_includes_backend_authorized_archive_card():
    import sufen.provider as provider

    task = SuFenTaskPackage.model_validate(_property_task(archiveContext={
        "companyId": "company-ZYJ",
        "authorizationId": "AUTH-P-1",
        "archive": {
            "id": "P-1",
            "displayName": "验收赵姐金融城房源",
            "fields": {"业主姓名": "赵姐", "底价": "620万"},
        },
    }))

    messages = provider.build_provider_messages("请读当前档案", task)
    system_message = messages[0]["content"]

    assert "后端已授权当前资料事实卡" in system_message
    assert "赵姐" in system_message
    assert "620万" in system_message
    assert "不得要求用户再提供站内ID" in system_message


def test_provider_retries_when_model_wrongly_requests_id_despite_task_package(monkeypatch):
    clear_delegation_nonce_cache()
    monkeypatch.setenv("SUFEN_PROVIDER_API_KEY", "provider-key")
    monkeypatch.setenv("SUFEN_DELEGATION_HMAC_SECRET", DELEGATION_SECRET)
    monkeypatch.setenv("SUFEN_BASE_URL", "https://provider.test/v1")
    monkeypatch.setenv("SUFEN_TAVILY_API_KEY", "sufen-tavily")

    import sufen.provider as provider

    calls = []

    def fake_post(_url, _headers, payload):
        calls.append(payload)
        if len(calls) == 1:
            content = {
                "answer": FAIL_CLOSED_MESSAGE,
                "evidenceUsed": [],
                "missingAuthorizationRequests": [{"reason": "missing_authorized_reference", "message": FAIL_CLOSED_MESSAGE}],
                "eventDrafts": [],
                "fieldPatchDrafts": [],
                "memoryPatch": None,
                "toolAudit": [],
            }
            return {"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]}

        joined_messages = "\n".join(message["content"] or "" for message in payload["messages"])
        assert "后端已授权当前资料事实卡" in joined_messages
        assert "上一轮输出错误地要求用户补站内ID" in joined_messages
        assert "赵姐" in joined_messages
        assert "620万" in joined_messages
        content = {
            "answer": "当前房源业主是赵姐，底价620万。",
            "evidenceUsed": [{"source": "taskPackage.archiveContext.archive", "summary": "读取当前档案事实"}],
            "missingAuthorizationRequests": [],
            "eventDrafts": [],
            "fieldPatchDrafts": [],
            "memoryPatch": None,
            "toolAudit": [],
        }
        return {"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]}

    monkeypatch.setattr(provider, "_post_chat_completions", fake_post)
    delegation = _signed_delegation_token(nonce="nonce-authorized-context-retry")
    task = SuFenTaskPackage.model_validate(_property_task(
        archiveContext={
            "companyId": "company-ZYJ",
            "authorizationId": "AUTH-P-1",
            "archive": {
                "id": "P-1",
                "displayName": "验收赵姐金融城房源",
                "fields": {"业主姓名": "赵姐", "底价": "620万"},
            },
        },
        delegationToken=delegation.model_dump(),
    ))

    response = answer_sufen("请根据当前档案复述业主姓名和底价", task=task, settings=load_settings())

    assert len(calls) == 2
    assert "赵姐" in response.answer
    assert "620万" in response.answer
    assert not response.missingAuthorizationRequests
    assert any(item.action == "authorized_context_retry" for item in response.toolAudit)


def test_provider_falls_back_to_task_package_when_retry_still_requests_id(monkeypatch):
    clear_delegation_nonce_cache()
    monkeypatch.setenv("SUFEN_PROVIDER_API_KEY", "provider-key")
    monkeypatch.setenv("SUFEN_DELEGATION_HMAC_SECRET", DELEGATION_SECRET)
    monkeypatch.setenv("SUFEN_BASE_URL", "https://provider.test/v1")
    monkeypatch.setenv("SUFEN_TAVILY_API_KEY", "sufen-tavily")

    import sufen.provider as provider

    calls = []

    def fake_post(_url, _headers, payload):
        calls.append(payload)
        content = {
            "answer": FAIL_CLOSED_MESSAGE,
            "evidenceUsed": [],
            "missingAuthorizationRequests": [{"reason": "missing_authorized_reference", "message": FAIL_CLOSED_MESSAGE}],
            "eventDrafts": [],
            "fieldPatchDrafts": [],
            "memoryPatch": None,
            "toolAudit": [],
        }
        return {"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]}

    monkeypatch.setattr(provider, "_post_chat_completions", fake_post)
    delegation = _signed_delegation_token(
        subject={"type": "after-sale", "id": "SH-1"},
        nonce="nonce-authorized-context-fallback",
    )
    task = SuFenTaskPackage.model_validate({
        "operator": {"userId": "1001", "name": "经纪人A", "role": "broker"},
        "subject": {"type": "after-sale", "id": "SH-1"},
        "scene": "售后维护",
        "archiveContext": {
            "companyId": "company-ZYJ",
            "authorizationId": "AUTH-SH-1",
            "archive": {
                "id": "SH-1",
                "displayName": "陈总售后",
                "fields": {"客户姓名": "陈总", "成交房源": "银泰泰悦湾", "售后重点": "节日问候和转介绍"},
            },
        },
        "scopedMemoryKey": "company-ZYJ/operators/1001/subjects/after-sale/SH-1",
        "delegationToken": delegation.model_dump(),
    })

    response = answer_sufen("请读当前售后档案", task=task, settings=load_settings())

    assert len(calls) == 2
    assert "陈总" in response.answer
    assert "银泰泰悦湾" in response.answer
    assert "转介绍" in response.answer
    assert not response.missingAuthorizationRequests
    assert response.memoryPatch is not None
    assert any(item.action == "authorized_context_fallback" for item in response.toolAudit)


def test_provider_falls_back_to_task_package_when_tool_call_is_rejected(monkeypatch):
    clear_delegation_nonce_cache()
    monkeypatch.setenv("SUFEN_PROVIDER_API_KEY", "provider-key")
    monkeypatch.setenv("SUFEN_DELEGATION_HMAC_SECRET", DELEGATION_SECRET)
    monkeypatch.setenv("SUFEN_BASE_URL", "https://provider.test/v1")
    monkeypatch.setenv("SUFEN_TAVILY_API_KEY", "sufen-tavily")

    import sufen.provider as provider

    def fake_post(_url, _headers, _payload):
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call-bad",
                        "type": "function",
                        "function": {"name": "not_allowed_tool", "arguments": "{}"},
                    }],
                }
            }]
        }

    monkeypatch.setattr(provider, "_post_chat_completions", fake_post)
    delegation = _signed_delegation_token(nonce="nonce-authorized-context-tool-fallback")
    task = SuFenTaskPackage.model_validate(_property_task(
        archiveContext={
            "companyId": "company-ZYJ",
            "authorizationId": "AUTH-P-1",
            "archive": {
                "id": "P-1",
                "displayName": "赵姐房源",
                "fields": {"业主姓名": "赵姐", "底价": "620万"},
            },
        },
        delegationToken=delegation.model_dump(),
    ))

    response = answer_sufen("请读当前档案", task=task, settings=load_settings())

    assert "赵姐" in response.answer
    assert "620万" in response.answer
    assert not response.missingAuthorizationRequests
    assert response.memoryPatch is not None
    assert any(item.action == "authorized_context_fallback" for item in response.toolAudit)


def test_provider_tool_call_loop_executes_whitelist_tool(monkeypatch):
    clear_delegation_nonce_cache()
    monkeypatch.setenv("SUFEN_PROVIDER_API_KEY", "provider-key")
    monkeypatch.setenv("SUFEN_DELEGATION_HMAC_SECRET", DELEGATION_SECRET)
    monkeypatch.setenv("SUFEN_BASE_URL", "https://provider.test/v1")
    monkeypatch.setenv("SUFEN_TAVILY_API_KEY", "sufen-tavily")

    import sufen.provider as provider

    calls = []
    archive_tool = provider.provider_tool_name("mystand.archive.read")
    kg_tool = provider.provider_tool_name("mystand.knowledge_graph.read")

    def fake_post(_url, _headers, payload):
        calls.append(payload)
        if len(calls) == 1:
            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "mystand_parse",
                                "arguments": json.dumps({"text": "AUTH-P-1 KGREF-property-maintenance"}),
                            },
                        }],
                    }
                }]
            }
        assert any(message["role"] == "tool" and message["tool_call_id"] == "call-1" for message in payload["messages"])
        content = {
            "answer": "tool loop final answer",
            "evidenceUsed": [],
            "missingAuthorizationRequests": [],
            "eventDrafts": [],
            "fieldPatchDrafts": [],
            "memoryPatch": None,
            "toolAudit": [],
        }
        return {"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]}

    monkeypatch.setattr(provider, "_post_chat_completions", fake_post)
    delegation = _signed_delegation_token(nonce="nonce-tool-loop")
    task = SuFenTaskPackage.model_validate(_property_task(delegationToken=delegation.model_dump()))
    response = answer_sufen("AUTH-P-1 KGREF-property-maintenance", task=task, settings=load_settings())

    assert response.answer == "tool loop final answer"
    assert len(calls) == 2
    assert any(item.tool == "mystand_parse" and item.action == "provider_tool_call" for item in response.toolAudit)
    assert any(item.tool == "provider.chat_completions" for item in response.toolAudit)


def test_provider_normalizes_model_evidence_shape(monkeypatch):
    clear_delegation_nonce_cache()
    monkeypatch.setenv("SUFEN_PROVIDER_API_KEY", "provider-key")
    monkeypatch.setenv("SUFEN_DELEGATION_HMAC_SECRET", DELEGATION_SECRET)
    monkeypatch.setenv("SUFEN_BASE_URL", "https://provider.test/v1")
    monkeypatch.setenv("SUFEN_TAVILY_API_KEY", "sufen-tavily")

    import sufen.provider as provider

    def fake_post(_url, _headers, _payload):
        content = {
            "answer": "normalized evidence answer",
            "evidenceUsed": [{"authorizationId": "AUTH-P-1", "keyPoint": "客户预算和区域明确"}],
            "missingAuthorizationRequests": [],
            "eventDrafts": [],
            "fieldPatchDrafts": [],
            "memoryPatch": None,
            "toolAudit": [],
        }
        return {"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]}

    monkeypatch.setattr(provider, "_post_chat_completions", fake_post)
    delegation = _signed_delegation_token(nonce="nonce-normalize-evidence")
    task = SuFenTaskPackage.model_validate(_property_task(delegationToken=delegation.model_dump()))
    response = answer_sufen("AUTH-P-1", task=task, settings=load_settings())

    assert response.answer == "normalized evidence answer"
    assert response.evidenceUsed[0].source == "AUTH-P-1"
    assert response.evidenceUsed[0].summary == "客户预算和区域明确"


def test_provider_normalizes_missing_authorization_shape(monkeypatch):
    clear_delegation_nonce_cache()
    monkeypatch.setenv("SUFEN_PROVIDER_API_KEY", "provider-key")
    monkeypatch.setenv("SUFEN_DELEGATION_HMAC_SECRET", DELEGATION_SECRET)
    monkeypatch.setenv("SUFEN_BASE_URL", "https://provider.test/v1")
    monkeypatch.setenv("SUFEN_TAVILY_API_KEY", "sufen-tavily")

    import sufen.provider as provider

    def fake_post(_url, _headers, _payload):
        content = {
            "answer": "结算需要单独授权后再核对。",
            "evidenceUsed": [],
            "missingAuthorizationRequests": [{
                "type": "settlement_card",
                "description": "需要结算卡权限后再做财务确认。",
            }],
            "eventDrafts": [],
            "fieldPatchDrafts": [],
            "memoryPatch": None,
            "toolAudit": [],
        }
        return {"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]}

    monkeypatch.setattr(provider, "_post_chat_completions", fake_post)
    delegation = _signed_delegation_token(nonce="nonce-normalize-missing-auth")
    task = SuFenTaskPackage.model_validate(_property_task(delegationToken=delegation.model_dump()))
    response = answer_sufen("明确问结算卡时缺权限怎么办", task=task, settings=load_settings())

    assert response.missingAuthorizationRequests[0].reason == "settlement_card"
    assert response.missingAuthorizationRequests[0].message == "需要结算卡权限后再做财务确认。"


def test_provider_normalizes_draft_and_audit_shapes(monkeypatch):
    clear_delegation_nonce_cache()
    monkeypatch.setenv("SUFEN_PROVIDER_API_KEY", "provider-key")
    monkeypatch.setenv("SUFEN_DELEGATION_HMAC_SECRET", DELEGATION_SECRET)
    monkeypatch.setenv("SUFEN_BASE_URL", "https://provider.test/v1")
    monkeypatch.setenv("SUFEN_TAVILY_API_KEY", "sufen-tavily")

    import sufen.provider as provider

    def fake_post(_url, _headers, _payload):
        content = {
            "answer": "normalized draft answer",
            "evidenceUsed": [],
            "missingAuthorizationRequests": [],
            "eventDrafts": [{"title": "约客户看房", "description": "明天上午确认金融城房源"}],
            "fieldPatchDrafts": [{"field": "维护要点", "after": "先确认预算弹性和看房节奏。"}],
            "memoryPatch": {
                "scope": "company-ZYJ/operators/1001/subjects/property/P-1",
                "businessFacts": ["客户关注金融城"],
                "brokerAdaptation": "经纪人需要先给数据再沟通",
                "openQuestions": "底价弹性待确认",
                "sourceRefs": "AUTH-P-1",
            },
            "toolAudit": [{"tool": "mystand_archive_read", "summary": "读取客户字段"}],
        }
        return {"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]}

    monkeypatch.setattr(provider, "_post_chat_completions", fake_post)
    delegation = _signed_delegation_token(nonce="nonce-normalize-drafts")
    task = SuFenTaskPackage.model_validate(_property_task(delegationToken=delegation.model_dump()))
    response = answer_sufen("AUTH-P-1", task=task, settings=load_settings())

    assert response.eventDrafts[0].name == "约客户看房"
    assert response.eventDrafts[0].body == "明天上午确认金融城房源"
    assert response.fieldPatchDrafts[0].field == "维护要点"
    assert response.fieldPatchDrafts[0].diff.startswith("-")
    assert "+先确认预算弹性和看房节奏。" in response.fieldPatchDrafts[0].diff
    assert response.memoryPatch is not None
    assert response.memoryPatch.scope == {}
    assert response.memoryPatch.businessFacts == ["客户关注金融城"]
    assert response.memoryPatch.brokerAdaptation == ["经纪人需要先给数据再沟通"]
    assert response.memoryPatch.openQuestions == ["底价弹性待确认"]
    assert response.memoryPatch.sourceRefs == ["AUTH-P-1"]
    assert response.toolAudit[0].tool == "mystand.archive.read"
    assert response.toolAudit[0].action == "provider_report"
    assert response.toolAudit[0].status == "读取客户字段"


def test_provider_tool_call_uses_task_bound_memory_archive_and_kg(monkeypatch):
    clear_delegation_nonce_cache()
    monkeypatch.setenv("SUFEN_PROVIDER_API_KEY", "provider-key")
    monkeypatch.setenv("SUFEN_DELEGATION_HMAC_SECRET", DELEGATION_SECRET)
    monkeypatch.setenv("SUFEN_BASE_URL", "https://provider.test/v1")
    monkeypatch.setenv("SUFEN_TAVILY_API_KEY", "sufen-tavily")

    import sufen.provider as provider

    calls = []
    archive_tool = provider.provider_tool_name("mystand.archive.read")
    kg_tool = provider.provider_tool_name("mystand.knowledge_graph.read")

    def fake_post(_url, _headers, payload):
        calls.append(payload)
        if len(calls) == 1:
            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-memory",
                                "type": "function",
                                "function": {
                                    "name": "sufen_memory_search",
                                    "arguments": json.dumps({"query": "底价"}),
                                },
                            },
                            {
                                "id": "call-archive",
                                "type": "function",
                                "function": {
                                    "name": archive_tool,
                                    "arguments": json.dumps({"authorizationId": "AUTH-P-1"}),
                                },
                            },
                            {
                                "id": "call-kg",
                                "type": "function",
                                "function": {
                                    "name": kg_tool,
                                    "arguments": json.dumps({"knowledgeGraphRef": "KGREF-property-maintenance"}),
                                },
                            },
                        ],
                    }
                }]
            }

        tool_messages = {message["name"]: json.loads(message["content"]) for message in payload["messages"] if message["role"] == "tool"}
        assert "/operators/1001/subjects/property/P-1/memory.json" in tool_messages["sufen_memory_search"]["path"]
        assert "/operators/1002/" not in tool_messages["sufen_memory_search"]["path"]
        assert "P-OTHER" not in tool_messages["sufen_memory_search"]["path"]
        assert tool_messages["mystand_archive_read"]["archive"]["baseInfo"]["title"] == "阳光花园三居"
        assert tool_messages["mystand_knowledge_graph_read"]["graph"]["name"] == "房源维护知识图谱"
        content = {
            "answer": "task-bound tools ok",
            "evidenceUsed": [],
            "missingAuthorizationRequests": [],
            "eventDrafts": [],
            "fieldPatchDrafts": [],
            "memoryPatch": None,
            "toolAudit": [],
        }
        return {"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]}

    monkeypatch.setattr(provider, "_post_chat_completions", fake_post)
    delegation = _signed_delegation_token(nonce="nonce-task-bound-tools")
    task = SuFenTaskPackage.model_validate(_property_task(delegationToken=delegation.model_dump()))
    response = answer_sufen("AUTH-P-1 KGREF-property-maintenance", task=task, settings=load_settings())

    assert response.answer == "task-bound tools ok"
    assert len(calls) == 2
    assert {item.tool for item in response.toolAudit} >= {
        "sufen_memory_search",
        "mystand.archive.read",
        "mystand.knowledge_graph.read",
    }


def test_provider_tool_call_memory_search_cross_scope_fails_closed(monkeypatch):
    clear_delegation_nonce_cache()
    monkeypatch.setenv("SUFEN_PROVIDER_API_KEY", "provider-key")
    monkeypatch.setenv("SUFEN_DELEGATION_HMAC_SECRET", DELEGATION_SECRET)
    monkeypatch.setenv("SUFEN_BASE_URL", "https://provider.test/v1")
    monkeypatch.setenv("SUFEN_TAVILY_API_KEY", "sufen-tavily")

    import sufen.provider as provider

    calls = []

    def fake_post(_url, _headers, payload):
        calls.append(payload)
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call-cross-memory",
                        "type": "function",
                        "function": {
                            "name": "sufen_memory_search",
                            "arguments": json.dumps({
                                "query": "底价",
                                "operatorUserId": "1002",
                                "subjectId": "P-OTHER",
                            }),
                        },
                    }],
                }
            }]
        }

    monkeypatch.setattr(provider, "_post_chat_completions", fake_post)
    delegation = _signed_delegation_token(nonce="nonce-cross-memory")
    task = SuFenTaskPackage.model_validate(_property_task(delegationToken=delegation.model_dump()))
    response = answer_sufen("AUTH-P-1", task=task, settings=load_settings())

    assert len(calls) == 1
    assert response.answer == FAIL_CLOSED_MESSAGE
    assert response.missingAuthorizationRequests[0].reason == "unauthorized_tool_call"
    assert "task scope mismatch" in response.toolAudit[0].status


def test_provider_tool_call_archive_read_ignores_forged_authorized_payload(monkeypatch):
    clear_delegation_nonce_cache()
    monkeypatch.setenv("SUFEN_PROVIDER_API_KEY", "provider-key")
    monkeypatch.setenv("SUFEN_DELEGATION_HMAC_SECRET", DELEGATION_SECRET)
    monkeypatch.setenv("SUFEN_BASE_URL", "https://provider.test/v1")
    monkeypatch.setenv("SUFEN_TAVILY_API_KEY", "sufen-tavily")

    import sufen.provider as provider

    calls = []

    def fake_post(_url, _headers, payload):
        calls.append(payload)
        if len(calls) == 1:
            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "call-archive-forged",
                            "type": "function",
                            "function": {
                                "name": "mystand.archive.read",
                                "arguments": json.dumps({
                                    "authorizationId": "AUTH-P-1",
                                    "authorizedPayload": {"title": "模型伪造资料", "forged": True},
                                }),
                            },
                        }],
                    }
                }]
            }
        tool_content = next(message["content"] for message in payload["messages"] if message.get("name") == "mystand.archive.read")
        assert "模型伪造资料" not in tool_content
        assert "forged" not in tool_content
        assert "阳光花园三居" in tool_content
        content = {
            "answer": "archive payload ignored",
            "evidenceUsed": [],
            "missingAuthorizationRequests": [],
            "eventDrafts": [],
            "fieldPatchDrafts": [],
            "memoryPatch": None,
            "toolAudit": [],
        }
        return {"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]}

    monkeypatch.setattr(provider, "_post_chat_completions", fake_post)
    delegation = _signed_delegation_token(nonce="nonce-forged-archive")
    task = SuFenTaskPackage.model_validate(_property_task(delegationToken=delegation.model_dump()))
    response = answer_sufen("AUTH-P-1", task=task, settings=load_settings())

    assert response.answer == "archive payload ignored"
    assert len(calls) == 2


def test_provider_tool_call_archive_read_rejects_non_task_ref(monkeypatch):
    clear_delegation_nonce_cache()
    monkeypatch.setenv("SUFEN_PROVIDER_API_KEY", "provider-key")
    monkeypatch.setenv("SUFEN_DELEGATION_HMAC_SECRET", DELEGATION_SECRET)
    monkeypatch.setenv("SUFEN_BASE_URL", "https://provider.test/v1")
    monkeypatch.setenv("SUFEN_TAVILY_API_KEY", "sufen-tavily")

    import sufen.provider as provider

    calls = []

    def fake_post(_url, _headers, payload):
        calls.append(payload)
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call-archive-other",
                        "type": "function",
                        "function": {
                            "name": "mystand.archive.read",
                            "arguments": json.dumps({"authorizationId": "AUTH-P-OTHER"}),
                        },
                    }],
                }
            }]
        }

    monkeypatch.setattr(provider, "_post_chat_completions", fake_post)
    delegation = _signed_delegation_token(nonce="nonce-archive-other")
    task = SuFenTaskPackage.model_validate(_property_task(delegationToken=delegation.model_dump()))
    response = answer_sufen("AUTH-P-1", task=task, settings=load_settings())

    assert len(calls) == 1
    assert response.answer == FAIL_CLOSED_MESSAGE
    assert "unauthorized_archive_ref" in response.toolAudit[0].status


def test_provider_tool_call_knowledge_graph_rejects_non_task_ref(monkeypatch):
    clear_delegation_nonce_cache()
    monkeypatch.setenv("SUFEN_PROVIDER_API_KEY", "provider-key")
    monkeypatch.setenv("SUFEN_DELEGATION_HMAC_SECRET", DELEGATION_SECRET)
    monkeypatch.setenv("SUFEN_BASE_URL", "https://provider.test/v1")
    monkeypatch.setenv("SUFEN_TAVILY_API_KEY", "sufen-tavily")

    import sufen.provider as provider

    calls = []

    def fake_post(_url, _headers, payload):
        calls.append(payload)
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call-kg-other",
                        "type": "function",
                        "function": {
                            "name": "mystand.knowledge_graph.read",
                            "arguments": json.dumps({"knowledgeGraphRef": "KGREF-other"}),
                        },
                    }],
                }
            }]
        }

    monkeypatch.setattr(provider, "_post_chat_completions", fake_post)
    delegation = _signed_delegation_token(nonce="nonce-kg-other")
    task = SuFenTaskPackage.model_validate(_property_task(delegationToken=delegation.model_dump()))
    response = answer_sufen("AUTH-P-1 KGREF-property-maintenance", task=task, settings=load_settings())

    assert len(calls) == 1
    assert response.answer == FAIL_CLOSED_MESSAGE
    assert "unauthorized_knowledge_graph_ref" in response.toolAudit[0].status


def test_provider_tool_call_loop_rejects_non_whitelist_tool(monkeypatch):
    clear_delegation_nonce_cache()
    monkeypatch.setenv("SUFEN_PROVIDER_API_KEY", "provider-key")
    monkeypatch.setenv("SUFEN_DELEGATION_HMAC_SECRET", DELEGATION_SECRET)
    monkeypatch.setenv("SUFEN_BASE_URL", "https://provider.test/v1")
    monkeypatch.setenv("SUFEN_TAVILY_API_KEY", "sufen-tavily")

    import sufen.provider as provider

    calls = []

    def fake_post(_url, _headers, payload):
        calls.append(payload)
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call-bad",
                        "type": "function",
                        "function": {"name": "terminal", "arguments": "{}"},
                    }],
                }
            }]
        }

    monkeypatch.setattr(provider, "_post_chat_completions", fake_post)
    delegation = _signed_delegation_token(nonce="nonce-bad-tool")
    task = SuFenTaskPackage.model_validate(_property_task(delegationToken=delegation.model_dump()))
    response = answer_sufen("AUTH-P-1", task=task, settings=load_settings())

    assert len(calls) == 1
    assert response.answer == FAIL_CLOSED_MESSAGE
    assert response.missingAuthorizationRequests[0].reason == "unauthorized_tool_call"
    assert "terminal" in response.toolAudit[0].status


def test_production_mode_requires_delegation_token_before_provider(monkeypatch):
    monkeypatch.setenv("SUFEN_PROVIDER_API_KEY", "provider-key")
    monkeypatch.setenv("SUFEN_DELEGATION_HMAC_SECRET", DELEGATION_SECRET)
    monkeypatch.setenv("SUFEN_BASE_URL", "https://provider.test/v1")

    import sufen.provider as provider

    called = {"provider": False}

    def fail_if_called(*_args, **_kwargs):
        called["provider"] = True
        raise AssertionError("provider must not be called without delegationToken")

    monkeypatch.setattr(provider, "_post_chat_completions", fail_if_called)
    task = SuFenTaskPackage.model_validate(_property_task())
    try:
        answer_sufen("AUTH-P-1", task=task, settings=load_settings())
    except ValueError as exc:
        assert "delegation token" in str(exc)
    else:
        raise AssertionError("production task without delegationToken must fail closed before provider")
    assert called["provider"] is False


def test_first_property_archive_scenario_outputs_drafts_and_memory_patch():
    task = SuFenTaskPackage.model_validate({
        "operator": {"userId": "1001", "name": "经纪人A", "role": "broker"},
        "subject": {"type": "property", "id": "P-1"},
        "scene": "房源维护",
        "archiveContext": {
            "companyId": "company-ZYJ",
            "baseInfo": {"title": "阳光花园三居", "askingPrice": "480万"},
            "propertyNote": "业主说先按原价挂一周。",
            "ownerIntent": "想换房，但对降价犹豫",
            "fiveDimensionScores": {"priceFlexibility": 2, "urgency": 3, "cooperation": 3},
            "eventSummary": ["上次沟通提到同小区成交价偏低", "近三天有两组看房但无明确报价"],
        },
        "brokerProfile": {"capabilityStage": "新手", "traits": ["需要话术"]},
        "knowledgeGraphRefs": ["KGREF-property-maintenance"],
        "scopedMemoryKey": "company-ZYJ/operators/1001/subjects/property/P-1",
    })
    response = answer_with_fake_provider("AUTH-P-1 KGREF-property-maintenance 帮我判断", task=task)
    payload = SuFenResponse.model_validate(response.model_dump())

    assert not payload.missingAuthorizationRequests
    assert payload.eventDrafts and payload.eventDrafts[0].draftOnly is True
    assert payload.fieldPatchDrafts and payload.fieldPatchDrafts[0].draftOnly is True
    assert "-业主说先按原价挂一周。" in payload.fieldPatchDrafts[0].diff
    assert "+SuFen策略建议" in payload.fieldPatchDrafts[0].diff
    assert payload.memoryPatch is not None
    assert payload.memoryPatch.draftOnly is True
    assert payload.memoryPatch.scope["operatorUserId"] == "1001"
    assert payload.memoryPatch.scope["subjectId"] == "P-1"
    assert payload.memoryPatch.memoryIndexText
    assert any(item.tool == "mystand.event.draft" for item in payload.toolAudit)
    assert any(item.tool == "mystand.field_patch_draft" for item in payload.toolAudit)
    assert any(item.tool == "sufen_memory_patch_draft" for item in payload.toolAudit)


def test_first_property_archive_scenario_respects_allowed_actions():
    task = SuFenTaskPackage.model_validate({
        "operator": {"userId": "1001", "name": "经纪人A", "role": "broker"},
        "subject": {"type": "property", "id": "P-1"},
        "scene": "房源维护",
        "archiveContext": {"baseInfo": {"title": "阳光花园三居"}},
        "knowledgeGraphRefs": ["KGREF-property-maintenance"],
        "allowedActions": ["analyze", "suggest"],
    })
    response = answer_with_fake_provider("AUTH-P-1 KGREF-property-maintenance", task=task)
    assert response.eventDrafts == []
    assert response.fieldPatchDrafts == []
    assert response.memoryPatch is None


def test_task_package_denied_actions_and_delegation_token():
    clear_delegation_nonce_cache()
    subject = {"type": "property", "id": "P-1"}
    delegation = _signed_delegation_token(subject=subject, nonce="nonce-safe")
    assert delegation.issuer == "mystand-core"
    assert delegation.audience == "sufen-agent"

    task = SuFenTaskPackage.model_validate({
        "operator": {"userId": "1001", "name": "经纪人A", "role": "broker"},
        "subject": subject,
        "scene": "房源维护",
        "allowedActions": ["analyze", "suggest"],
        "delegationToken": delegation.model_dump(),
    })
    ensure_safe_actions(task, delegation_secret=DELEGATION_SECRET)

    mismatched_token_task = task.model_copy(update={
        "operator": task.operator.model_copy(update={"userId": "1002"}),
    })
    try:
        ensure_safe_actions(mismatched_token_task)
    except ValueError as exc:
        assert "operatorUserId" in str(exc)
    else:
        raise AssertionError("delegation token operator mismatch must fail closed")

    unsafe = task.model_copy(update={"deniedActions": ["directWrite"]})
    try:
        ensure_safe_actions(unsafe)
    except ValueError as exc:
        assert "crossUserRead" in str(exc)
        assert "externalSend" in str(exc)
        assert "rawDbAccess" in str(exc)
    else:
        raise AssertionError("missing denied actions must fail closed")


def test_delegation_token_security_checks():
    subject = {"type": "property", "id": "P-1"}

    clear_delegation_nonce_cache()
    expired = _signed_delegation_token(
        subject=subject,
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        nonce="nonce-expired",
    )
    expired_task = SuFenTaskPackage.model_validate({
        **_property_task(),
        "allowedActions": ["analyze", "suggest"],
        "delegationToken": expired.model_dump(),
    })
    try:
        ensure_safe_actions(expired_task, delegation_secret=DELEGATION_SECRET)
    except ValueError as exc:
        assert "expired" in str(exc)
    else:
        raise AssertionError("expired token must fail closed")

    clear_delegation_nonce_cache()
    wrong_subject = _signed_delegation_token(subject={"type": "property", "id": "P-2"}, nonce="nonce-subject")
    wrong_subject_task = SuFenTaskPackage.model_validate({
        **_property_task(),
        "allowedActions": ["analyze", "suggest"],
        "delegationToken": wrong_subject.model_dump(),
    })
    try:
        ensure_safe_actions(wrong_subject_task, delegation_secret=DELEGATION_SECRET)
    except ValueError as exc:
        assert "subject" in str(exc)
    else:
        raise AssertionError("subject mismatch must fail closed")

    clear_delegation_nonce_cache()
    wrong_signature = _signed_delegation_token(subject=subject, nonce="nonce-signature")
    wrong_signature = wrong_signature.model_copy(update={"signature": "hmac-sha256:bad"})
    wrong_signature_task = SuFenTaskPackage.model_validate({
        **_property_task(),
        "allowedActions": ["analyze", "suggest"],
        "delegationToken": wrong_signature.model_dump(),
    })
    try:
        ensure_safe_actions(wrong_signature_task, delegation_secret=DELEGATION_SECRET)
    except ValueError as exc:
        assert "signature" in str(exc)
    else:
        raise AssertionError("bad signature must fail closed")

    clear_delegation_nonce_cache()
    replay = _signed_delegation_token(subject=subject, nonce="nonce-replay")
    replay_task = SuFenTaskPackage.model_validate({
        **_property_task(),
        "allowedActions": ["analyze", "suggest"],
        "delegationToken": replay.model_dump(),
    })
    ensure_safe_actions(replay_task, delegation_secret=DELEGATION_SECRET)
    try:
        ensure_safe_actions(replay_task, delegation_secret=DELEGATION_SECRET)
    except ValueError as exc:
        assert "nonce" in str(exc)
    else:
        raise AssertionError("nonce replay must fail closed")


def test_health_and_fake_chat_smoke(monkeypatch):
    monkeypatch.setenv("SUFEN_PROVIDER", "deepseek")
    monkeypatch.setenv("SUFEN_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("SUFEN_SERVICE_API_KEY", "service-key")
    monkeypatch.setenv("SUFEN_FAKE_PROVIDER", "1")
    client = TestClient(create_app())
    headers = {"Authorization": "Bearer service-key"}
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["service"] == "sufen-agent"

    missing_task = client.post("/v1/chat", headers=headers, json={"query": "AUTH-P-1 这个房源怎么维护"})
    assert missing_task.status_code == 200
    assert missing_task.json()["missingAuthorizationRequests"][0]["reason"] == "missing_task_package"

    chat = client.post("/v1/chat", headers=headers, json={
        "query": "AUTH-P-1 KGREF-property-maintenance 这个房源怎么维护",
        "taskPackage": {
            "operator": {"userId": "1001", "name": "经纪人A", "role": "broker"},
            "subject": {"type": "property", "id": "P-1"},
            "scene": "房源维护",
            "archiveContext": {
                "baseInfo": {"title": "阳光花园三居"},
                "eventSummary": ["最近一次沟通需要确认底价"],
            },
            "brokerProfile": {"capabilityStage": "新手"},
            "knowledgeGraphRefs": ["KGREF-property-maintenance"],
            "scopedMemoryKey": "company-ZYJ/operators/1001/subjects/property/P-1",
        },
    })
    assert chat.status_code == 200
    assert "answer" in chat.json()
    assert chat.json()["eventDrafts"]

    unsafe = client.post("/v1/chat", headers=headers, json={
        "query": "AUTH-P-1 KGREF-property-maintenance",
        "taskPackage": {
            "operator": {"userId": "1001", "name": "经纪人A", "role": "broker"},
            "subject": {"type": "property", "id": "P-1"},
            "scene": "房源维护",
            "knowledgeGraphRefs": ["KGREF-property-maintenance"],
            "deniedActions": ["directWrite"],
        },
    })
    assert unsafe.status_code == 200
    unsafe_payload = unsafe.json()
    assert unsafe_payload["answer"] == FAIL_CLOSED_MESSAGE
    assert unsafe_payload["missingAuthorizationRequests"][0]["reason"] == "unsafe_task_package"
    assert unsafe_payload["toolAudit"][0]["status"].startswith("rejected:")


def test_http_chat_requires_service_api_key(monkeypatch):
    monkeypatch.setenv("SUFEN_SERVICE_API_KEY", "service-key")
    monkeypatch.setenv("SUFEN_PROVIDER_API_KEY", "provider-key")
    monkeypatch.setenv("SUFEN_FAKE_PROVIDER", "1")
    client = TestClient(create_app())

    assert client.post("/v1/chat", json={"query": "AUTH-P-1"}).status_code == 401
    assert client.post(
        "/v1/chat",
        headers={"Authorization": "Bearer wrong"},
        json={"query": "AUTH-P-1"},
    ).status_code == 403
    assert client.post(
        "/v1/chat",
        headers={"X-SuFen-API-Key": "provider-key"},
        json={"query": "AUTH-P-1"},
    ).status_code == 403

    ok = client.post(
        "/v1/chat",
        headers={"X-SuFen-API-Key": "service-key"},
        json={"query": "AUTH-P-1"},
    )
    assert ok.status_code == 200
    assert ok.json()["missingAuthorizationRequests"][0]["reason"] == "missing_task_package"


def test_http_service_key_and_provider_key_are_separate(monkeypatch):
    clear_delegation_nonce_cache()
    monkeypatch.setenv("SUFEN_SERVICE_API_KEY", "service-key")
    monkeypatch.setenv("SUFEN_PROVIDER_API_KEY", "provider-key")
    monkeypatch.setenv("SUFEN_DELEGATION_HMAC_SECRET", DELEGATION_SECRET)
    monkeypatch.setenv("SUFEN_BASE_URL", "https://provider.test/v1")
    monkeypatch.setenv("SUFEN_FAKE_PROVIDER", "0")
    monkeypatch.setenv("SUFEN_TAVILY_API_KEY", "sufen-tavily")

    import sufen.provider as provider

    captured = {}

    def fake_post(_url, headers, _payload):
        captured["provider_auth"] = headers["Authorization"]
        content = {
            "answer": "split key ok",
            "evidenceUsed": [],
            "missingAuthorizationRequests": [],
            "eventDrafts": [],
            "fieldPatchDrafts": [],
            "memoryPatch": None,
            "toolAudit": [],
        }
        return {"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]}

    monkeypatch.setattr(provider, "_post_chat_completions", fake_post)
    client = TestClient(create_app())
    assert client.post(
        "/v1/chat",
        headers={"Authorization": "Bearer provider-key"},
        json={"query": "AUTH-P-1"},
    ).status_code == 403

    delegation = _signed_delegation_token(nonce="nonce-split-key")
    ok = client.post(
        "/v1/chat",
        headers={"Authorization": "Bearer service-key"},
        json={
            "query": "AUTH-P-1 KGREF-property-maintenance",
            "taskPackage": _property_task(delegationToken=delegation.model_dump()),
        },
    )
    assert ok.status_code == 200
    assert ok.json()["answer"] == "split key ok"
    assert captured["provider_auth"] == "Bearer provider-key"


def test_http_chat_fails_closed_when_server_key_unconfigured(monkeypatch):
    monkeypatch.delenv("SUFEN_API_KEY", raising=False)
    monkeypatch.delenv("SUFEN_SERVICE_API_KEY", raising=False)
    monkeypatch.setenv("SUFEN_FAKE_PROVIDER", "1")
    monkeypatch.setattr(sufen_config, "_candidate_env_files", lambda: [])
    client = TestClient(create_app())
    response = client.post(
        "/v1/chat",
        headers={"Authorization": "Bearer anything"},
        json={"query": "AUTH-P-1"},
    )
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "sufen_service_api_key_not_configured"


def test_sufen_version_smoke():
    result = subprocess.run(
        [sys.executable, "-m", "sufen.cli", "--version"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "SuFen-Agent v" in result.stdout


def test_sufen_without_args_enters_local_chat(monkeypatch, capsys):
    import builtins
    import sufen.cli as sufen_cli

    monkeypatch.setenv("SUFEN_PROVIDER", "deepseek")
    monkeypatch.setenv("SUFEN_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("SUFEN_FAKE_PROVIDER", "0")
    monkeypatch.setattr(sufen_config, "_candidate_env_files", lambda: [])
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    lines = iter(["你好，你是谁", "AUTH-P-1", ""])

    def fake_input(_: str) -> str:
        try:
            value = next(lines)
        except StopIteration:
            raise EOFError from None
        if value == "":
            raise EOFError
        return value

    monkeypatch.setattr(builtins, "input", fake_input)

    assert sufen_cli.main([]) == 0
    output = capsys.readouterr().out
    assert "SuFen v" in output
    assert "素分 SuFen · My Stand 档案军师" in output
    assert "SuFen │ terminal │ deepseek-v4-pro │ context: none" in output
    assert "Recent activity" not in output
    assert "Welcome back!" not in output
    assert "我是 SuFen，My Stand 的档案军师" in output
    assert "AUTH-P-1" in output
    assert "裸终端入口需要 My Stand 后端注入 taskPackage" in output
    assert "missingAuthorizationRequests" not in output


def test_sufen_terminal_prompt_names_entry():
    from sufen.terminal_ui import terminal_prompt

    assert terminal_prompt() == "sufen> "


def test_sufen_chat_unsafe_task_package_fails_closed(tmp_path):
    task_path = tmp_path / "unsafe-task.json"
    task_path.write_text(json.dumps({
        "operator": {"userId": "1001", "name": "经纪人A", "role": "broker"},
        "subject": {"type": "property", "id": "P-1"},
        "scene": "房源维护",
        "knowledgeGraphRefs": ["KGREF-property-maintenance"],
        "deniedActions": ["directWrite"],
    }), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "sufen.cli",
            "chat",
            "--fake",
            "-q",
            "AUTH-P-1 KGREF-property-maintenance",
            "--task-package",
            str(task_path),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["answer"] == FAIL_CLOSED_MESSAGE
    assert payload["missingAuthorizationRequests"][0]["reason"] == "unsafe_task_package"


def test_runbook_v1_chat_smoke_includes_delegation_token():
    runbook = (REPO_ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    assert "/v1/chat" in runbook
    assert "delegationToken" in runbook
    assert "AgentDelegationToken" in runbook
    assert "sign_delegation_token" in runbook
    assert "SUFEN_DELEGATION_HMAC_SECRET" in runbook
    assert "sufen-smoke-local" not in runbook
    assert "import uuid" in runbook
    assert '"nonce": "sufen-smoke-" + uuid.uuid4().hex' in runbook
    assert "--data-binary @/tmp/sufen-smoke-request.json" in runbook


def test_first_release_metadata_exposes_only_sufen_surfaces():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = pyproject["project"]["optional-dependencies"]
    assert set(extras) == {"all", "dev", "web"}
    assert extras["all"] == ["sufen-agent[web]"]
    assert pyproject["project"]["scripts"] == {"sufen": "sufen.cli:main"}
    plugin_data = pyproject["tool"]["setuptools"]["package-data"]["plugins"]
    assert plugin_data == [
        "web/tavily/plugin.yaml",
        "model-providers/deepseek/__init__.py",
        "model-providers/deepseek/plugin.yaml",
    ]

    package_json = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
    for key in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies", "workspaces"):
        assert key not in package_json

    package_lock = json.loads((REPO_ROOT / "package-lock.json").read_text(encoding="utf-8"))
    assert set(package_lock["packages"]) == {""}

    candidates = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.splitlines()
    forbidden_prefixes = (
        "plugins/web/brave_free/",
        "plugins/web/ddgs/",
        "plugins/web/exa/",
        "plugins/web/firecrawl/",
        "plugins/web/parallel/",
        "plugins/web/searxng/",
        "plugins/web/xai/",
        "plugins/model-providers/alibaba",
        "plugins/model-providers/anthropic",
        "plugins/model-providers/openrouter",
        "plugins/model-providers/xai",
        "plugins/model-providers/nous",
        "plugins/model-providers/gemini",
    )
    assert not [path for path in candidates if path.startswith(forbidden_prefixes)]
    assert not [path for path in candidates if LEGACY_LOWER in path.lower() or "nous" in path.lower()]
    py_modules = pyproject["tool"]["setuptools"]["py-modules"]
    assert "sufen_constants" in py_modules
    assert "sufen_logging" in py_modules
    assert not [module for module in py_modules if LEGACY_LOWER in module.lower() or "nous" in module.lower()]
