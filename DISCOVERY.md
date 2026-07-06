# Discovery

## Current Baseline

- Local repository: `SuFen_Agent`
- Public command: `sufen`
- Python requirement: `>=3.11,<3.14`
- Local validation Python: `3.11.15`
- Node present locally: `v26.0.0`
- npm present locally: `11.12.1`

## Useful Runtime Areas

- Core conversation loop: `agent/conversation_loop.py`
- System prompt assembly: `agent/system_prompt.py`, `agent/prompt_builder.py`
- Tool registry and executor: `tools/registry.py`, `agent/tool_executor.py`, `model_tools.py`
- Toolset selection: `toolsets.py`
- Memory/session interfaces: `agent/memory_manager.py`, `sufen/memory.py`, `sufen/session.py`
- SuFen policy: `sufen/policy/system.md`
- SuFen CLI/API: `sufen/cli.py`, `sufen/server.py`
- SuFen My Stand tools: `tools/sufen_mystand_tools.py`

## First Release Decisions

- Keep the inherited loop/provider/compression/tool registry intact.
- Add SuFen as a strict business layer with its own CLI, env, memory scope, output contract, and tool whitelist.
- Keep My Stand production data disconnected.
- Keep old runtime state and dependency directories out of Git.
- Exclude old public surfaces from the first-release deliverable by default:
  desktop, TUI, website, translated README files, legacy installers, messaging
  platforms, bundled skills, old broad tests, old CLI/gateway packages, and
  platform gateways.
- Wheel packaging is governed by `pyproject.toml` and `MANIFEST.in`; the old
  `setup.py` path has been removed from the local tree.
