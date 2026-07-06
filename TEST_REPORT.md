# Test Report

Verification date: 2026-07-06

Verification source: GitHub fresh clone, not the original local candidate tree.

Validated Git commit:

```text
64272336f4db4c45c500692dde20f72c56697519
```

Fresh clone directory:

```text
/tmp/sufen-agent-fresh
```

Note: the host did not have a global `uv` command, so `uv` was installed into `/tmp/sufen-uv-venv` only for verification. All project checks below were run with `uv run --python 3.11` from the GitHub fresh clone.

## Fresh Clone Setup

```bash
rm -rf /tmp/sufen-agent-fresh /tmp/sufen-wheel-check /tmp/sufen-uv-cache /tmp/sufen-uv-python
git clone git@github.com:52707407SXG/SuFen_Agent.git /tmp/sufen-agent-fresh
cd /tmp/sufen-agent-fresh
git rev-parse HEAD
git ls-files sufen
```

Result:

```text
HEAD = 64272336f4db4c45c500692dde20f72c56697519
sufen/ package present with 19 tracked files, including cli.py, server.py, build.py, policy/system.md, prompt/identity.py, memory.py, output.py, task_package.py, and property_strategy.py.
```

## Required Commands

```bash
uv run --python 3.11 python -c "import sufen; print(sufen.__file__)"
```

Result:

```text
/private/tmp/sufen-agent-fresh/sufen/__init__.py
passed
```

```bash
uv run --python 3.11 python -m pip install -e ".[all]"
```

Result:

```text
Successfully installed sufen-agent-0.1.0
passed
```

```bash
uv run --python 3.11 pytest tests/sufen -q
```

Result:

```text
31 passed in 1.46s
```

```bash
uv run --python 3.11 sufen --version
```

Result:

```text
SuFen-Agent v0.1.0
passed
```

```bash
uv run --python 3.11 python -m pip wheel . --no-deps --no-build-isolation -w /tmp/sufen-wheel-check
```

Result:

```text
Successfully built sufen-agent
wheel: sufen_agent-0.1.0-py3-none-any.whl
passed
```

## Supplemental Runtime Checks

```bash
uv run --python 3.11 sufen chat -q "AUTH-P-1 KGREF-property-maintenance 这个房源怎么维护" --task-package /tmp/sufen-property-task.json
```

Result:

```text
cli-json-ok {'answer': True, 'eventDrafts': True, 'fieldPatchDrafts': True, 'memoryPatch': True, 'toolAudit': True}
passed
```

```bash
uv run --python 3.11 sufen serve --host 127.0.0.1 --port 8791
curl -s http://127.0.0.1:8791/health
```

Result:

```json
{"ok":true,"service":"sufen-agent","version":"0.1.0","provider":"deepseek","model":"deepseek-v4-pro"}
```

```bash
curl -s http://127.0.0.1:8791/v1/chat \
  -H 'content-type: application/json' \
  -d '{"query":"AUTH-P-1 这个房源怎么维护"}'
```

Result:

```text
missingAuthorizationRequests[0].reason = missing_task_package
toolAudit[0].action = require_backend_injected_scope
passed
```

```bash
curl -s http://127.0.0.1:8791/v1/chat \
  -H 'content-type: application/json' \
  -d @/tmp/sufen-property-request.json
```

Result:

```text
http-chat-ok {'answer': True, 'eventDrafts': True, 'fieldPatchDrafts': True, 'memoryPatch': True, 'toolAudit': True}
passed
```

```bash
SUFEN_AGENT_MODE=1 SUFEN_TAVILY_API_KEY=sufen-tavily \
uv run --python 3.11 python -c '<tool whitelist and memory scope probe>'
```

Result:

```text
tool-whitelist-ok ['mystand.archive.read', 'mystand.auth.resolve', 'mystand.event.draft', 'mystand.field_patch_draft', 'mystand.knowledge_graph.read', 'mystand_parse', 'sufen_memory_patch_draft', 'sufen_memory_search', 'web_extract', 'web_search']
memory-scope-ok /var/lib/sufen-agent/memory/company-ZYJ/operators/1001/subjects/property/P-1/memory.json
passed
```

This confirms `SUFEN_AGENT_MODE=1` exposes only the 10 SuFen first-release tools and does not expose `terminal`, `read_file`, `write_file`, `patch`, browser automation, `execute_code`, `delegate_task`, `cronjob`, or `computer_use`. It also confirms `sufen_memory_search` no longer exposes `memoryRoot` or `admin` in its schema and ignores those keys if a model attempts to pass them.

## Brand And Secret Checks

```bash
uv run --python 3.11 python scripts/sufen_rebrand_check.py
uv run --python 3.11 python scripts/sufen_secret_scan.py
python -c 'import pathlib; legacy=("Her"+"mes").lower(); hits=[str(p) for p in pathlib.Path(".").rglob("*") if p.is_file() and legacy in p.read_text("utf-8", errors="ignore").lower()]; print(len(hits))'
```

Result:

```text
sufen-rebrand-check ok
sufen-secret-scan ok
old-brand search: 0 matches
passed
```

## Fixes Verified

- `sufen/` is now tracked in GitHub and imports successfully from a fresh clone.
- `.gitignore` no longer excludes the `sufen/` package.
- `uv run --python 3.11 python -m pip install -e ".[all]"` is reproducible because `pip==26.0.1` is pinned.
- `uv run --python 3.11 pytest tests/sufen -q` is reproducible because the uv dev dependency group includes pytest.
- `python -m pip wheel . --no-deps --no-build-isolation` is reproducible because the uv dev dependency group includes `setuptools==81.0.0`.
- `sufen_memory_search` is scope-locked: no model-selected `memoryRoot`, no model-selected `admin` path.

## Remaining Risk

- The validated code commit is `64272336f4db4c45c500692dde20f72c56697519`. This report is updated after that validation run; any report-only commit after this point should not change runtime code.
- Localhost server verification required sandbox escalation only because the sandbox blocks binding `127.0.0.1:8791` by default.
