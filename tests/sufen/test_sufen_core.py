from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

from fastapi.testclient import TestClient

from agent.system_prompt import build_system_prompt, build_system_prompt_parts
from agent.transports.chat_completions import ChatCompletionsTransport
from sufen.auth import FAIL_CLOSED_MESSAGE, extract_authorization_refs
import sufen.config as sufen_config
from sufen.config import load_settings
from sufen.fake_provider import answer_with_fake_provider
from sufen.memory import draft_memory_patch, memory_path
from sufen.output import SuFenResponse
from sufen.server import create_app
from sufen.session import SuFenSession
from sufen.task_package import AgentDelegationToken, SuFenTaskPackage, ensure_safe_actions
from toolsets import SUFEN_TOOL_NAMES, get_toolset_names, resolve_toolset, validate_toolset
from tools.registry import registry
import tools.sufen_mystand_tools  # noqa: F401


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_sufen_env_does_not_reuse_miner_key(monkeypatch):
    monkeypatch.delenv("SUFEN_API_KEY", raising=False)
    monkeypatch.setenv("MYSTAND_MINER_API_KEY", "miner-secret")
    monkeypatch.setattr(sufen_config, "_candidate_env_files", lambda: [])
    settings = load_settings()
    assert settings.api_key == ""


def test_sufen_loads_local_dotenv_and_keeps_process_env_priority(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SUFEN_API_KEY", raising=False)
    monkeypatch.delenv("SUFEN_PORT", raising=False)
    monkeypatch.setattr(sufen_config, "_candidate_env_files", lambda: [tmp_path / ".env"])
    (tmp_path / ".env").write_text(
        "\n".join([
            "SUFEN_API_KEY=from-dotenv",
            "SUFEN_PORT=8799",
            "MYSTAND_MINER_API_KEY=must-not-load",
        ]),
        encoding="utf-8",
    )

    settings = load_settings()
    assert settings.api_key == "from-dotenv"
    assert settings.port == 8799

    monkeypatch.setenv("SUFEN_API_KEY", "from-process-env")
    assert load_settings().api_key == "from-process-env"


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
    assert "资料优先" in parts["stable"]
    assert "验收要求" in parts["stable"]


def test_sufen_policy_builds_without_inherited_runtime(monkeypatch):
    monkeypatch.setenv("SUFEN_AGENT_MODE", "1")
    parts = build_system_prompt_parts(_AgentStub())
    assert "你是 SuFen" in parts["stable"]
    assert "资料优先" in parts["stable"]
    assert "实际 LLM 请求的 system message" in parts["stable"]


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


def test_sufen_system_prompt_skips_inherited_soul_and_subscription(monkeypatch):
    import agent.system_prompt as system_prompt

    monkeypatch.setenv("SUFEN_AGENT_MODE", "1")
    monkeypatch.setattr(system_prompt, "_ra", lambda: _LegacyRuntimeStub)
    agent = _AgentStub()
    agent.load_soul_identity = True
    agent.valid_tool_names = list(SUFEN_TOOL_NAMES)
    system_message = build_system_prompt(agent)

    forbidden = ["SuFen", "sufen", "portal billing", "OpenClaw", "Xiaoban"]
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
    subject = {"type": "property", "id": "P-1"}
    token = AgentDelegationToken.model_validate({
        "actorAgent": "lucan",
        "operatorUserId": "1001",
        "subject": subject,
        "allowedActions": ["analyze", "suggest"],
        "expiresAt": "2026-07-06T23:59:00Z",
        "nonce": "nonce-1",
        "signature": "test-signature",
    })
    assert token.issuer == "mystand-core"
    assert token.audience == "sufen-agent"

    task = SuFenTaskPackage.model_validate({
        "operator": {"userId": "1001", "name": "经纪人A", "role": "broker"},
        "subject": subject,
        "scene": "房源维护",
        "allowedActions": ["analyze", "suggest"],
        "delegationToken": token.model_dump(),
    })
    ensure_safe_actions(task)

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


def test_health_and_fake_chat_smoke(monkeypatch):
    monkeypatch.setenv("SUFEN_PROVIDER", "deepseek")
    monkeypatch.setenv("SUFEN_MODEL", "deepseek-v4-pro")
    client = TestClient(create_app())
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["service"] == "sufen-agent"

    missing_task = client.post("/v1/chat", json={"query": "AUTH-P-1 这个房源怎么维护"})
    assert missing_task.status_code == 200
    assert missing_task.json()["missingAuthorizationRequests"][0]["reason"] == "missing_task_package"

    chat = client.post("/v1/chat", json={
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

    unsafe = client.post("/v1/chat", json={
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


def test_sufen_version_smoke():
    result = subprocess.run(
        [sys.executable, "-m", "sufen.cli", "--version"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "SuFen-Agent v" in result.stdout


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
    assert not [path for path in candidates if "sufen" in path.lower() or "nous" in path.lower()]
    py_modules = pyproject["tool"]["setuptools"]["py-modules"]
    assert "sufen_constants" in py_modules
    assert "sufen_logging" in py_modules
    assert not [module for module in py_modules if "sufen" in module.lower() or "nous" in module.lower()]
