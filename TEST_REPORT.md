# Test Report

Verification date: 2026-07-06

Verification source: GitHub fresh clone, not the original local candidate tree.

Runtime validation commit:

```text
2e49b7447e517b04e6b0fa97243f9a13042a182a
```

Fresh clone directory:

```text
/tmp/sufen-agent-fresh-next
```

Note: this machine uses a temporary `uv` runtime under `/tmp/sufen-uv-venv` for verification. All project checks below were run from the GitHub fresh clone with `uv run --python 3.11`.

## Fresh Clone Setup

```bash
rm -rf /tmp/sufen-agent-fresh-next /tmp/sufen-wheel-check-next
git clone git@github.com:52707407SXG/SuFen_Agent.git /tmp/sufen-agent-fresh-next
cd /tmp/sufen-agent-fresh-next
git rev-parse HEAD
```

Result:

```text
HEAD = 2e49b7447e517b04e6b0fa97243f9a13042a182a
```

## Required Commands

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
40 passed in 1.11s
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
uv run --python 3.11 python -c "import sufen; print(sufen.__file__)"
```

Result:

```text
/private/tmp/sufen-agent-fresh-next/sufen/__init__.py
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

## Brand And Secret Checks

```bash
python3 scripts/sufen_rebrand_check.py
python3 scripts/sufen_secret_scan.py
python3 -c 'import pathlib; legacy=("Her"+"mes").lower(); hits=[str(p) for p in pathlib.Path(".").rglob("*") if p.is_file() and legacy in p.read_text("utf-8", errors="ignore").lower()]; print(len(hits))'
```

Result:

```text
sufen-rebrand-check ok
sufen-secret-scan ok
old-brand search: 0 matches
passed
```

## Coverage Confirmed By Tests

- `sufen --version` works from the installed command entry.
- `GET /health` is covered by the FastAPI smoke test.
- Production chat path does not call `answer_with_fake_provider`.
- Production mode requires `delegationToken` before the provider is called.
- Provider tool-calling loop executes whitelist tools and sends tool results back to the provider.
- Non-whitelist provider tool calls fail closed and are not executed.
- `/v1/chat` requires `SUFEN_SERVICE_API_KEY`.
- Provider requests use `SUFEN_PROVIDER_API_KEY`; service and provider keys can differ.
- `SUFEN_API_KEY` remains only as a deprecated compatibility fallback.
- delegationToken HMAC signature, expiry, operator, subject, allowedActions, and nonce replay are checked.
- scoped memory uses operator and subject scope, and does not expose model-selected `memoryRoot` or admin paths.
- event drafts and field patch drafts remain draft-only.

## Remaining Risk

- The runtime validation commit is `2e49b7447e517b04e6b0fa97243f9a13042a182a`. This report may be committed after that validation; report-only commits do not change runtime code.
- Provider verification uses local stubs in unit tests instead of a paid external model endpoint, so it proves request path, auth header selection, system policy injection, taskPackage injection, tool loop behavior, and whitelist enforcement without spending external tokens.
