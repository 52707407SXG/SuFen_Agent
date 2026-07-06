#!/usr/bin/env python3
"""Live SuFen dialogue probe for the 20-case / 100-turn policy set.

Run only when provider credentials are available. It uses synthetic task
packages and does not call My Stand writeback APIs.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.check_sufen_dialogue_policy import DIALOGUE_CASES, _task_for_case  # noqa: E402
from sufen.chat import answer_sufen  # noqa: E402
from sufen.config import load_settings  # noqa: E402
from sufen.output import SuFenResponse  # noqa: E402
from sufen.task_package import AgentDelegationToken, sign_delegation_token  # noqa: E402


WRONG_NAME_PATTERNS = ("素" + "芬", "塑" + "封", "塑" + "分")
LEGACY_BRAND_RE = re.compile(r"(?i)\b" + "her" + "mes" + r"\b")
VERBOSE_CHAR_LIMIT = 900
FAIL_CLOSED_SNIPPETS = ("缺资料", "提供站内ID", "没有授权资料", "无法读取当前档案")


def _signed_task(case_index: int):
    settings = load_settings()
    if not settings.provider_api_key:
        raise RuntimeError("SUFEN_PROVIDER_API_KEY is required for live dialogue probe")
    if not settings.delegation_hmac_secret:
        raise RuntimeError("SUFEN_DELEGATION_HMAC_SECRET is required for live dialogue probe")
    task = _task_for_case(DIALOGUE_CASES[case_index])
    archive_context = dict(task.archiveContext)
    archive_context.setdefault("archiveSummary", "合成验证摘要：本轮只用于 SuFen 对话策略验收，不含真实客户或业务数据。")
    if task.subject.type == "broker":
        archive_context.setdefault("broker", {
            "id": task.subject.id,
            "name": "合成经纪人",
            "role": "broker",
            "yearPerformanceSummary": "今年业绩结构含签二手、出房源、签租赁、省心租、签新房和衍生业绩；无结算明细。",
        })
        archive_context.setdefault("archiveRows", [
            {"month": "2026-01", "type": "签二手", "amount": 12000},
            {"month": "2026-02", "type": "签租赁", "amount": 3600},
            {"month": "2026-03", "type": "省心租", "amount": 4200},
            {"month": "2026-04", "type": "出房源", "amount": 9800},
            {"month": "2026-05", "type": "签新房", "amount": 8000},
            {"month": "2026-06", "type": "衍生业绩", "amount": 2600},
        ])
    else:
        archive_context.setdefault("archive", {
            "id": task.subject.id,
            "type": task.subject.type,
            "displayName": "合成档案",
            "fields": {
                "状态": "合成验证",
                "维护重点": "先判断真实意图，再按需读取资料。",
                "最近变化": "对方态度和业务节奏有变化，需要轻判断。",
            },
        })

    token = AgentDelegationToken.model_validate({
        "actorAgent": "sufen-live-dialogue-probe",
        "operatorUserId": task.operator.userId,
        "subject": task.subject.model_dump(mode="json"),
        "allowedActions": task.allowedActions,
        "expiresAt": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        "nonce": f"sufen-dialogue-live-{case_index}-{datetime.now(timezone.utc).timestamp()}",
        "signature": "pending",
    })
    signed_token = token.model_copy(update={"signature": sign_delegation_token(token, settings.delegation_hmac_secret)})
    return task.model_copy(update={
        "archiveContext": archive_context,
        "delegationToken": signed_token,
    })


def _prompt_for_case(case_index: int) -> str:
    case = DIALOGUE_CASES[case_index]
    return "\n".join([
        "以下是同一段对话里的用户连续表达。请只回答最后这轮最合适的一步，不要写完整报告。",
        f"case={case.case_id}",
        f"relation={case.relation}",
        *[f"用户第{index + 1}句：{turn}" for index, turn in enumerate(case.turns)],
    ])


def _answer_text(response: SuFenResponse) -> str:
    return re.sub(r"\s+", "", response.answer or "")


def _check_response(case_index: int, response: SuFenResponse) -> list[str]:
    case = DIALOGUE_CASES[case_index]
    answer = response.answer or ""
    compact = _answer_text(response)
    failures: list[str] = []
    if not answer.strip():
        failures.append("empty answer")
    if len(answer) > VERBOSE_CHAR_LIMIT:
        failures.append(f"answer too verbose: {len(answer)} > {VERBOSE_CHAR_LIMIT}")
    for pattern in WRONG_NAME_PATTERNS:
        if pattern in answer:
            failures.append(f"wrong name variant leaked: {pattern}")
    if LEGACY_BRAND_RE.search(answer):
        failures.append("legacy brand leaked")
    if case.relation == "self_to_self" and ("给刚哥汇报" in compact or "向刚哥汇报" in compact):
        failures.append("self conversation treated as manager report")
    if case.case_id != "settlement_explicit" and "点没点结算" in compact:
        failures.append("settlement click detail appeared without explicit settlement request")
    if any(snippet in compact for snippet in FAIL_CLOSED_SNIPPETS) and not response.evidenceUsed:
        failures.append("failed closed despite synthetic authorized context")
    return failures


def main() -> int:
    failures: list[str] = []
    rows: list[dict[str, object]] = []
    total_turns = sum(len(case.turns) for case in DIALOGUE_CASES)
    for index, case in enumerate(DIALOGUE_CASES):
        task = _signed_task(index)
        response = answer_sufen(_prompt_for_case(index), task=task, settings=load_settings())
        case_failures = _check_response(index, response)
        rows.append({
            "case": case.case_id,
            "relation": case.relation,
            "answerChars": len(response.answer or ""),
            "evidenceCount": len(response.evidenceUsed),
            "missingCount": len(response.missingAuthorizationRequests),
            "failures": case_failures,
        })
        for failure in case_failures:
            failures.append(f"{case.case_id}: {failure}")

    print(json.dumps({
        "ok": not failures,
        "dialogueCases": len(DIALOGUE_CASES),
        "userTurns": total_turns,
        "rows": rows,
    }, ensure_ascii=False, sort_keys=True, indent=2))
    if failures:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
