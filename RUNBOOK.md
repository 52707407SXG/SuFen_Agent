# SuFen-Agent Runbook

## Fresh Clone Setup

```bash
git clone git@github.com:52707407SXG/SuFen_Agent.git
cd SuFen_Agent

python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip setuptools wheel
pip install -e ".[all]"

cp .env.example .env
```

Fill only SuFen-specific values in `.env`:

```bash
SUFEN_PROVIDER=deepseek
SUFEN_MODEL=deepseek-v4-pro
SUFEN_SERVICE_API_KEY=
SUFEN_PROVIDER_API_KEY=
SUFEN_BASE_URL=
SUFEN_DELEGATION_HMAC_SECRET=
SUFEN_TAVILY_API_KEY=
SUFEN_MEMORY_ROOT=/var/lib/sufen-agent/memory
SUFEN_BIND_HOST=127.0.0.1
SUFEN_PORT=8791
SUFEN_FAKE_PROVIDER=0
```

`SUFEN_API_KEY` is a deprecated compatibility fallback for older local runs only.
Use separate service and provider keys for deployment.

## Start

```bash
sufen --version
sufen
sufen chat -q "AUTH-P-1001 这个业主现在该怎么跟" --task-package /path/to/task-package.json
sufen serve
```

`sufen` without arguments opens the human terminal chat entry with the startup
card, model line, and `sufen>` prompt. The systemd service must continue to use
`sufen serve`.

Health:

```bash
curl http://127.0.0.1:8791/health
```

Task-package chat smoke:

```bash
set -a
source .env
set +a

python - <<'PY' > /tmp/sufen-smoke-request.json
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

from sufen.task_package import AgentDelegationToken, sign_delegation_token

task = {
    "operator": {"userId": "1001", "name": "经纪人A", "role": "broker"},
    "subject": {"type": "property", "id": "P-1"},
    "scene": "房源维护",
    "archiveContext": {
        "companyId": "company-ZYJ",
        "authorizationId": "AUTH-P-1",
        "baseInfo": {"title": "阳光花园三居", "askingPrice": "480万"},
        "propertyNote": "业主说先按原价挂一周。",
        "ownerIntent": "想换房，但对降价犹豫",
        "fiveDimensionScores": {"priceFlexibility": 2, "urgency": 3},
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
delegation = AgentDelegationToken.model_validate({
    "actorAgent": "lucan",
    "operatorUserId": "1001",
    "subject": task["subject"],
    "allowedActions": ["analyze", "suggest", "eventDraft", "fieldPatchDraft", "memoryPatch"],
    "expiresAt": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    "nonce": "sufen-smoke-" + uuid.uuid4().hex,
    "signature": "pending",
})
task["delegationToken"] = delegation.model_copy(update={
    "signature": sign_delegation_token(delegation, os.environ["SUFEN_DELEGATION_HMAC_SECRET"])
}).model_dump(mode="json")

print(json.dumps({
    "query": "AUTH-P-1 KGREF-property-maintenance 这个房源怎么维护",
    "taskPackage": task,
}, ensure_ascii=False))
PY

curl -s http://127.0.0.1:8791/v1/chat \
  -H 'content-type: application/json' \
  -H "Authorization: Bearer $SUFEN_SERVICE_API_KEY" \
  --data-binary @/tmp/sufen-smoke-request.json
```

Local fake-provider dry-run:

```bash
sufen chat --fake -q "AUTH-P-1 KGREF-property-maintenance 这个房源怎么维护" --task-package /path/to/task-package.json
```

## Optional Node Workspace

Node is not required to run SuFen v0.1.0. The root `package.json` is retained only for future asset/tooling work. It declares no dependencies, and the root `package-lock.json` is intentionally minimal.

```bash
npm install
```

## Pre-Handoff Checks

```bash
python scripts/sufen_rebrand_check.py
python scripts/sufen_secret_scan.py
pytest tests/sufen
sufen --version
sufen chat --fake -q "AUTH-P-1 KGREF-property-maintenance 这个房源怎么维护" --task-package /path/to/task-package.json
python -m pip wheel . --no-deps --no-build-isolation -w /tmp/sufen-wheel-check
```

Do not upload GitHub until the user explicitly approves upload.
