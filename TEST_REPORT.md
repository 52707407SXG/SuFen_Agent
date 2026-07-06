# Test Report

Local verification on 2026-07-06. GitHub upload has not been performed.

## Passed

- `.venv/bin/python scripts/sufen_rebrand_check.py` -> `sufen-rebrand-check ok`
- `.venv/bin/python scripts/sufen_secret_scan.py` -> `sufen-secret-scan ok`
- `.venv/bin/python -m pytest tests/sufen -q` -> `30 passed`
- `.venv/bin/python -m compileall -q ...` over SuFen packages and retained runtime modules -> passed
- `.venv/bin/sufen --version` -> `SuFen-Agent v0.1.0`
- `.venv/bin/python -m pip install -e '.[all]' --no-build-isolation` -> installed `sufen-agent-0.1.0`
- `UV_CACHE_DIR=.uv-cache ./.local/bin/uv lock` -> refreshed `uv.lock` after removing non-SuFen optional extras from first-release metadata
- `package-lock.json` check -> root package only, no Node dependencies or inherited workspace packages
- Python metadata check -> optional extras are exactly `all`, `dev`, and `web`; `[all]` only installs `sufen-agent[web]`
- Fresh clone env check -> local `.env` values are loaded for `SUFEN_*`, process env still wins, and non-SuFen keys are ignored
- SuFen web env isolation check -> SuFen mode uses `SUFEN_TAVILY_API_KEY` for Tavily from process env or local `.env`, and ignores generic `TAVILY_API_KEY`
- Tavily provider request check -> plugin-level Tavily requests also resolve `SUFEN_TAVILY_API_KEY` from process env or local `.env`
- SuFen runtime tool schema check -> `get_tool_definitions(enabled_toolsets=["sufen"])` exposes exactly the 10 first-release whitelist tools when `SUFEN_TAVILY_API_KEY` is present
- SuFen closed-runtime check -> `SUFEN_AGENT_MODE=1` makes `all` resolve only to `sufen`, hides inherited toolsets, skips inherited plugin discovery, and registers only the 10 whitelist tools in `model_tools`
- Provider registry check -> `SUFEN_AGENT_MODE=1` discovers only the reviewed DeepSeek provider profile
- Unsafe task-package check -> HTTP `/v1/chat` and `sufen chat --task-package ...` return structured fail-closed JSON instead of raising when denied safety actions are missing
- System prompt safety check -> SuFen mode skips inherited SOUL identity and subscription/portal guidance while keeping SuFen policy in the actual system message
- Direct SuFen system prompt build check -> `SUFEN_AGENT_MODE=1` builds without importing inherited `run_agent` runtime and includes `你是 SuFen`, `资料优先`, and the actual system-message验收要求
- `git ls-files --cached --others --exclude-standard` candidate scan -> 108 first-release files; no legacy README, website, installer, TUI, desktop, skills, old tests, old CLI/gateway package, platform gateway paths, old terminal/code/browser/message tools, or non-reviewed web/model-provider plugin directories
- Clean candidate import from `/private/tmp/sufen-candidate-final.331bCy` -> core modules import with `SUFEN_AGENT_MODE=1`, and `sufen_cli` modules loaded: `[]`
- `.venv/bin/python -m pip wheel . --no-deps --no-build-isolation -w /private/tmp/sufen-wheel-round2-default` from the full local tree -> built `sufen_agent-0.1.0-py3-none-any.whl`; SuFen build filter cleaned stale `build/lib` output before packaging
- Wheel content check -> 92 files; includes `sufen/property_strategy.py`, `sufen/policy/system.md`, `sufen/time.py`, the reviewed DeepSeek provider, and the Tavily web provider; excludes old CLI package, gateway package, TUI, desktop, website, installer scripts, old platform plugins, optional skills, old broad entry scripts, old terminal/code/browser/message tools, approval/managed gateway helpers, non-Tavily web plugins, non-reviewed model-provider plugins, and the inherited time module
- Wheel brand/path check -> 0 hits for old agent identity strings, old command package name, old rebrand reference, old core module names, non-SuFen key names, and removed inherited managed-subscription/portal wording
- Wheel smoke check -> `sufen --version` returns `SuFen-Agent v0.1.0`, SuFen policy is present in a built system message, and no old CLI package is imported
- Wheel isolation check -> after installing into `/private/tmp/sufen-wheel-install-round2-default` and removing editable finders, blocked modules such as `tools.terminal_tool`, `tools.browser_tool`, `tools.code_execution_tool`, `agent.lsp`, `agent.pet`, `toolset_distributions`, and `trajectory_compressor` are not importable
- Wheel metadata check -> `Provides-Extra` is only `dev`, `web`, and `all`
- `GET /health` on local server -> passed with `{"ok":true,"service":"sufen-agent","version":"0.1.0"}`; latest local smoke used fake provider/model on port 8793
- `POST /v1/chat` without `taskPackage` -> structured fail-closed JSON with `missingAuthorizationRequests`
- `POST /v1/chat` with a fake property task package -> structured JSON with strategy answer, event draft, field diff draft, memoryPatch, and tool audit
- Chat-completions request payload probe -> SuFen policy appears in the actual system message sent through request kwargs
- `sufen chat -q "帮我分析这个房源 AUTH-P-1 KGREF-property-maintenance"` -> structured JSON with `answer`, `evidenceUsed`, `missingAuthorizationRequests`, `eventDrafts`, `fieldPatchDrafts`, `memoryPatch`, and `toolAudit`

## Notes

- The localhost server smoke required sandbox escalation only to bind and call `127.0.0.1`.
- Broad deletion of inherited UI/docs/plugin directories was not performed in this pass; first-release delivery now excludes those public surfaces through `.gitignore`, `MANIFEST.in`, and default-closed install paths.
- Some inherited internal compatibility symbols remain in retained core helpers. They are kept to preserve the mature conversation/provider/compression loop, while SuFen CLI/API, docs, env template, default toolset, provider discovery, system policy, and first-release wheel entry paths are SuFen-branded.
- GitHub upload has not been performed.
