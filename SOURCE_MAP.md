# Source Map

## SuFen-Owned Layer

- `sufen/config.py`: SUFEN-only configuration and home path.
- `sufen/auth.py`: AUTH/OUT/KGREF/ref_/knowledge reference parsing and fail-closed message.
- `sufen/policy/system.md`: stable system-level business policy.
- `sufen/prompt/identity.py`: SuFen identity block.
- `sufen/task_package.py`: My Stand task package and delegation token models.
- `sufen/memory.py`: scoped memory path and patch draft helpers.
- `sufen/output.py`: structured JSON output contract.
- `sufen/property_strategy.py`: first-release owner property archive strategy generator.
- `sufen/session.py`: SuFen-native JSONL session/transcript helpers.
- `sufen/time.py`: SuFen-owned timezone-aware clock used by prompt and compression paths.
- `sufen/fake_provider.py`: deterministic fake provider for smoke tests.
- `sufen/provider.py`: production OpenAI-compatible provider path with SuFen system policy, taskPackage, whitelist tool schemas, and scoped memory constraints.
- `sufen/chat.py`: explicit fake-vs-production routing; production is default, fake is opt-in.
- `sufen/cli.py`: public `sufen` command.
- `sufen/server.py`: FastAPI app with `/health` and task-package-gated `/v1/chat`.
- `sufen/build.py`: build-time whitelist filter so ignored upstream reference files cannot enter the SuFen wheel.
- `tools/sufen_mystand_tools.py`: My Stand tool whitelist handlers, draft-only.
- `sufen_constants.py`: retained core constants under a SuFen-named module.
- `sufen_logging.py`: retained logging setup under a SuFen-named module.
- `pyproject.toml`: first-release Python package metadata, only exposing `all`, `dev`, and `web` extras; packaged web provider surface is Tavily-only and build output is filtered through `sufen.build.SuFenBuildPy`.
- `package.json` / `package-lock.json`: dependency-free Node placeholder for future tooling.

## Retained Runtime Chassis

- `agent/conversation_loop.py`: mature conversation/tool-calling loop.
- `agent/system_prompt.py`: prompt assembly; SuFen policy is injected into stable tier.
- `agent/prompt_builder.py`: default identity fallback now describes SuFen.
- `model_tools.py`, `tools/registry.py`, `toolsets.py`: tool registry and schema filtering.
- `agent/memory_manager.py`: retained memory provider interface.
- `sufen/server.py`: SuFen first-release HTTP API.

## Delivery Boundary

The upstream desktop, TUI, website, translated README files, installer scripts,
old CLI/gateway platform packages, bundled skills, old broad tests, non-Tavily
web providers, and other non-SuFen public surfaces are excluded through
`.gitignore` and from wheel packaging. Optional Python extras and the root Node
lock are also narrowed so those surfaces are not exposed as install targets.
Inherited files remain in the local working tree only as reference material
until a later review explicitly promotes or removes them.

## Verification

- `scripts/sufen_rebrand_check.py`
- `scripts/sufen_secret_scan.py`
- `tests/sufen/test_sufen_core.py`
