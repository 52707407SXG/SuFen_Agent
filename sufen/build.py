"""Build-time packaging filters for SuFen-Agent.

The local development tree may keep ignored upstream reference files around.
The published wheel must still contain only the reviewed SuFen first-release
runtime surface.
"""

from __future__ import annotations

from pathlib import Path
import shutil

from setuptools.command.build_py import build_py


ALLOWED_TOP_LEVEL_MODULES = {
    "model_tools",
    "sufen_constants",
    "sufen_logging",
    "toolsets",
    "utils",
}

ALLOWED_PACKAGE_MODULES = {
    "agent": {
        "__init__",
        "auxiliary_client",
        "chat_completion_helpers",
        "codex_responses_adapter",
        "context_compressor",
        "context_engine",
        "conversation_compression",
        "conversation_loop",
        "display",
        "error_classifier",
        "iteration_budget",
        "jiter_preload",
        "lmstudio_reasoning",
        "memory_manager",
        "memory_provider",
        "message_sanitization",
        "model_metadata",
        "moonshot_schema",
        "process_bootstrap",
        "prompt_builder",
        "prompt_caching",
        "redact",
        "retry_utils",
        "runtime_cwd",
        "secret_scope",
        "skill_commands",
        "skill_preprocessing",
        "skill_utils",
        "system_prompt",
        "tool_dispatch_helpers",
        "tool_executor",
        "tool_guardrails",
        "tool_result_classification",
        "trajectory",
        "turn_context",
        "turn_retry_state",
        "usage_pricing",
        "web_search_provider",
    },
    "agent.transports": {"__init__", "base", "chat_completions", "types"},
    "plugins": {"__init__", "plugin_utils"},
    "plugins.web": {"__init__"},
    "plugins.web.tavily": {"__init__", "provider"},
    "providers": {"__init__", "base"},
    "sufen": {
        "__init__",
        "auth",
        "cli",
        "compat",
        "config",
        "fake_provider",
        "memory",
        "output",
        "property_strategy",
        "server",
        "session",
        "task_package",
        "time",
    },
    "sufen.policy": {"__init__", "sufen_operating_policy"},
    "sufen.prompt": {"__init__", "identity"},
    "tools": {
        "__init__",
        "budget_config",
        "debug_helpers",
        "registry",
        "skill_provenance",
        "sufen_mystand_tools",
        "thread_context",
        "threat_patterns",
        "tool_result_storage",
        "url_safety",
        "web_tools",
    },
}


class SuFenBuildPy(build_py):
    """Restrict wheel modules to the reviewed SuFen first-release surface."""

    def run(self):
        build_lib = Path(self.build_lib)
        for name in [
            "agent",
            "plugins",
            "providers",
            "sufen",
            "tools",
            "model_tools.py",
            "sufen_constants.py",
            "sufen_logging.py",
            "toolsets.py",
            "utils.py",
            "toolset_distributions.py",
            "trajectory_compressor.py",
        ]:
            target = build_lib / name
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
        super().run()

    def find_modules(self):
        modules = super().find_modules()
        return [
            module
            for module in modules
            if module[0] or module[1] in ALLOWED_TOP_LEVEL_MODULES
        ]

    def find_package_modules(self, package, package_dir):
        allowed_modules = ALLOWED_PACKAGE_MODULES.get(package)
        if allowed_modules is None:
            return []
        return [
            module
            for module in super().find_package_modules(package, package_dir)
            if module[1] in allowed_modules
        ]
