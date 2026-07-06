# Test Report

Verification date: 2026-07-06

Verification source: GitHub fresh clone, not the original local candidate tree.

Runtime validation commit:

```text
4fc30aecaabce4da81e5e6f6d8344473f6ac9bc3
```

Fresh clone directory:

```text
/tmp/sufen-agent-fresh-next
```

Note: this machine did not have a global `uv` command. `uv` was installed into `/tmp/sufen-uv-venv` only for verification. All project checks below were run from the GitHub fresh clone with `uv run --python 3.11`.

## Fresh Clone Setup

```bash
rm -rf /tmp/sufen-agent-fresh-next /tmp/sufen-wheel-check-next /tmp/sufen-uv-cache-next /tmp/sufen-uv-python-next
git clone git@github.com:52707407SXG/SuFen_Agent.git /tmp/sufen-agent-fresh-next
cd /tmp/sufen-agent-fresh-next
git rev-parse HEAD
git ls-files sufen
```

Result:

```text
HEAD = 4fc30aecaabce4da81e5e6f6d8344473f6ac9bc3
sufen/ package present, including chat.py, provider.py, cli.py, server.py, build.py, policy/system.md, prompt/identity.py, task_package.py, memory.py, output.py, and property_strategy.py.
```

## Required Commands

```bash
uv run --python 3.11 python -c "import sufen; print(sufen.__file__)"
```

Result:

```text
/private/tmp/sufen-agent-fresh-next/sufen/__init__.py
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
35 passed in 1.54s
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
uv run --python 3.11 python -m pip wheel . --no-deps --no-build-isolation -w /tmp/sufen-wheel-check-next
```

Result:

```text
Successfully built sufen-agent
wheel: sufen_agent-0.1.0-py3-none-any.whl
passed
```

## Production Provider Verification

A local OpenAI-compatible provider stub was started on `127.0.0.1:8899`. SuFen was started with production mode enabled:

```bash
SUFEN_PROVIDER=deepseek
SUFEN_MODEL=deepseek-v4-pro
SUFEN_API_KEY=<test-service-key>
SUFEN_BASE_URL=http://127.0.0.1:8899/v1
SUFEN_FAKE_PROVIDER=0
SUFEN_TAVILY_API_KEY=sufen-tavily
uv run --python 3.11 sufen serve --host 127.0.0.1 --port 8792
```

Provider call evidence:

```text
real-provider-http-ok {'answer': True, 'eventDrafts': True, 'fieldPatchDrafts': True, 'memoryPatch': True, 'toolAudit': True}
provider-request-ok {'path': '/v1/chat/completions', 'tool_count': 10, 'has_policy': True, 'has_task': True}
```

This proves default `/v1/chat` production mode sent a real OpenAI-compatible provider request and did not use `sufen.fake_provider`. The captured provider request included:

- SuFen system policy in the system message.
- My Stand `taskPackage` in the user message.
- 10 SuFen whitelist tool schemas only.
- scoped memory constraints that forbid model-selected `memoryRoot` and admin paths.

## HTTP Authentication

```bash
curl -s -o /tmp/sufen-no-key.out -w '%{http_code}' http://127.0.0.1:8792/v1/chat ...
curl -s -o /tmp/sufen-wrong-key.out -w '%{http_code}' http://127.0.0.1:8792/v1/chat -H 'Authorization: Bearer wrong' ...
curl -s http://127.0.0.1:8792/health
```

Result:

```text
missing API key -> 401
wrong API key -> 403
/health -> {"ok":true,"service":"sufen-agent","version":"0.1.0","provider":"deepseek","model":"deepseek-v4-pro"}
```

With the correct `Authorization: Bearer <test-service-key>` header:

```text
/v1/chat with fake property taskPackage -> real-provider-http-ok
```

With the correct `X-SuFen-API-Key: <test-service-key>` header but no `taskPackage`:

```text
missing-task-fail-closed-ok missing_task_package
```

## Tool And Memory Boundary

```bash
SUFEN_AGENT_MODE=1 SUFEN_TAVILY_API_KEY=sufen-tavily \
uv run --python 3.11 python -c '<tool whitelist and memory scope probe>'
```

Result:

```text
tool-whitelist-ok ['mystand.archive.read', 'mystand.auth.resolve', 'mystand.event.draft', 'mystand.field_patch_draft', 'mystand.knowledge_graph.read', 'mystand_parse', 'sufen_memory_patch_draft', 'sufen_memory_search', 'web_extract', 'web_search']
memory-scope-ok /var/lib/sufen-agent/memory/company-ZYJ/operators/1001/subjects/property/P-1/memory.json
```

This confirms `SUFEN_AGENT_MODE=1` does not expose `terminal`, `read_file`, `write_file`, `patch`, browser automation, `execute_code`, `delegate_task`, `cronjob`, or `computer_use`. It also confirms `sufen_memory_search` does not expose `memoryRoot` or `admin` in schema and ignores those keys if a model attempts to pass them.

## Security Unit Tests

Covered by `pytest tests/sufen -q`:

- Production chat path does not call `answer_with_fake_provider`.
- `--fake` / `SUFEN_FAKE_PROVIDER=1` remains available for tests and dry-runs.
- `/v1/chat` requires `Authorization: Bearer <SUFEN_API_KEY>` or `X-SuFen-API-Key`.
- Missing request key returns 401; wrong request key returns 403.
- Empty configured `SUFEN_API_KEY` makes production `/v1/chat` fail closed.
- delegationToken HMAC signature is verified with `SUFEN_DELEGATION_HMAC_SECRET`.
- Expired token, operator mismatch, subject mismatch, signature error, and nonce replay all fail closed.

## Brand And Secret Checks

```bash
python scripts/sufen_rebrand_check.py
python scripts/sufen_secret_scan.py
python -c 'import pathlib; legacy=("Her"+"mes").lower(); hits=[str(p) for p in pathlib.Path(".").rglob("*") if p.is_file() and legacy in p.read_text("utf-8", errors="ignore").lower()]; print(len(hits))'
```

Result:

```text
sufen-rebrand-check ok
sufen-secret-scan ok
old-brand search: 0 matches
passed
```

## Remaining Risk

- The runtime validation commit is `4fc30aecaabce4da81e5e6f6d8344473f6ac9bc3`. This report may be committed after that validation; report-only commits do not change runtime code.
- The provider verification used a local OpenAI-compatible stub instead of a paid external model endpoint, so it proves the production request path, request shape, auth header, system policy injection, taskPackage injection, and tool whitelist without spending external tokens.
- Localhost server verification required sandbox escalation because the sandbox blocks binding/listening on `127.0.0.1` by default.
