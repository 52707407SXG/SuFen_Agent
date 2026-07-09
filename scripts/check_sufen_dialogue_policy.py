#!/usr/bin/env python3
"""Deterministic guard for SuFen's dialogue-system policy.

This does not call the provider. It protects the stable system-prompt contract
that makes live provider behavior possible: relationship sensing, Beijing-time
grounding, on-demand loading, performance vocabulary, and non-chatty pacing.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sufen.prompt.identity import build_sufen_identity_block  # noqa: E402
from sufen.provider import build_provider_messages  # noqa: E402
from sufen.task_package import SuFenTaskPackage  # noqa: E402


@dataclass(frozen=True)
class DialogueCase:
    case_id: str
    operator: dict[str, object]
    subject: dict[str, str]
    scene: str
    relation: str
    turns: tuple[str, ...]


DIALOGUE_CASES: tuple[DialogueCase, ...] = (
    DialogueCase(
        "manager_reviews_broker",
        {"userId": "52707407", "name": "刚哥", "role": "admin", "isGangGe": True},
        {"type": "broker", "id": "ZYJ010"},
        "个人业务档案",
        "manager_to_broker",
        (
            "晚上好，先别急着给我报表。",
            "我想听你判断这个人最近状态是不是稳。",
            "上半年业绩还行，但我担心他下半年没抓手。",
            "先说你认为最关键的一个矛盾。",
            "如果要监督，先从哪儿下手？",
        ),
    ),
    DialogueCase(
        "broker_self_check",
        {"userId": "ZYJ010", "name": "经纪人甲", "role": "broker"},
        {"type": "broker", "id": "ZYJ010"},
        "个人业务档案",
        "self_to_self",
        (
            "我自己最近有点乱。",
            "我感觉客户也带了，房源也看了，就是没结果。",
            "你别跟领导汇报我，直接跟我说我卡在哪儿。",
            "如果只能改一个习惯，你建议我先改什么？",
            "我听得懂，你说重点就行。",
        ),
    ),
    DialogueCase(
        "store_manager_reviews_broker",
        {"userId": "ZYJ005", "name": "店长甲", "role": "manager"},
        {"type": "broker", "id": "ZYJ011"},
        "个人业务档案",
        "manager_to_broker",
        (
            "这个经纪人最近总来问我。",
            "我分不清他是真想进步，还是没事做。",
            "你看资料时别扫结算卡。",
            "我只想先知道他的业务重心在哪里。",
            "如果要带教，话该怎么说才不伤人？",
        ),
    ),
    DialogueCase(
        "broker_small_talk",
        {"userId": "ZYJ012", "name": "经纪人乙", "role": "broker"},
        {"type": "broker", "id": "ZYJ012"},
        "个人业务档案",
        "self_to_self",
        (
            "你还在吗？",
            "我就是随便聊两句。",
            "今天感觉有点累。",
            "你不用马上给我安排任务。",
            "能不能先陪我捋捋心情？",
        ),
    ),
    DialogueCase(
        "broker_performance_structure",
        {"userId": "ZYJ013", "name": "经纪人丙", "role": "broker"},
        {"type": "broker", "id": "ZYJ013"},
        "个人业务档案",
        "self_to_self",
        (
            "我今年业绩结构是不是偏了？",
            "租赁和省心租我做得多。",
            "签二手很少，新房也不稳定。",
            "你先别看结算明细。",
            "只帮我判断突破方向。",
        ),
    ),
    DialogueCase(
        "owner_property_price",
        {"userId": "ZYJ014", "name": "经纪人丁", "role": "broker"},
        {"type": "property", "id": "P-010"},
        "房源维护",
        "broker_to_owner_archive",
        (
            "这个业主价格就是不降。",
            "市场价摆在那里，他就是不认。",
            "你先别只看价格。",
            "我想知道他这个人怎么沟通。",
            "下一通电话怎么开口？",
        ),
    ),
    DialogueCase(
        "property_media_boundary",
        {"userId": "ZYJ015", "name": "经纪人戊", "role": "broker"},
        {"type": "property", "id": "P-011"},
        "房源维护",
        "broker_to_property",
        (
            "这个房子照片多。",
            "我不是问装修细节。",
            "我想先判断它有没有维护价值。",
            "别把图片全读一遍。",
            "先给我一个轻判断。",
        ),
    ),
    DialogueCase(
        "client_follow_up",
        {"userId": "ZYJ016", "name": "经纪人己", "role": "broker"},
        {"type": "client", "id": "C-010"},
        "客源维护",
        "broker_to_client_archive",
        (
            "这个客户看了几套都不表态。",
            "我不知道他是真没看上还是预算不够。",
            "你先看客户反馈。",
            "不要把所有房源都扫出来。",
            "我需要下一步跟进方式。",
        ),
    ),
    DialogueCase(
        "after_sale_referral",
        {"userId": "ZYJ017", "name": "经纪人庚", "role": "broker"},
        {"type": "after-sale", "id": "S-010"},
        "售后维护",
        "broker_to_after_sale",
        (
            "这个成交客户还能不能转介绍？",
            "我不想硬要资源。",
            "先判断关系温度。",
            "再说什么时候联系合适。",
            "话术别太销售。",
        ),
    ),
    DialogueCase(
        "late_night_signal",
        {"userId": "ZYJ018", "name": "经纪人辛", "role": "broker"},
        {"type": "broker", "id": "ZYJ018"},
        "个人业务档案",
        "self_to_self",
        (
            "这么晚我还没睡。",
            "今天客户又没定。",
            "你别给我贴标签。",
            "我只是想知道还有没有救。",
            "你慢慢说，但别太长。",
        ),
    ),
    DialogueCase(
        "admin_asks_for_plan",
        {"userId": "52707407", "name": "刚哥", "role": "admin", "isGangGe": True},
        {"type": "broker", "id": "ZYJ019"},
        "个人业务档案",
        "manager_to_broker",
        (
            "我想看这个人三季度怎么盯。",
            "不是要完整报告。",
            "你先给管理抓手。",
            "如果他不配合，再说下一步。",
            "先别生成事件。",
        ),
    ),
    DialogueCase(
        "broker_asks_graph_stage",
        {"userId": "ZYJ020", "name": "经纪人壬", "role": "broker"},
        {"type": "broker", "id": "ZYJ020"},
        "个人业务档案",
        "self_to_self",
        (
            "我现在算哪个阶段？",
            "如果知识图谱没加载，就别装作看了。",
            "你可以先按已知资料判断。",
            "我想知道下一阶段该练什么。",
            "别一次给太多动作。",
        ),
    ),
    DialogueCase(
        "settlement_explicit",
        {"userId": "52707407", "name": "刚哥", "role": "admin", "isGangGe": True},
        {"type": "broker", "id": "ZYJ021"},
        "个人业务档案",
        "manager_to_broker",
        (
            "这次我明确问结算。",
            "你可以按授权看结算相关资料。",
            "先区分业绩分析和财务确认。",
            "不要把两件事混在一起。",
            "给我一个核对顺序。",
        ),
    ),
    DialogueCase(
        "voice_typo_normalization",
        {"userId": "52707407", "name": "刚哥", "role": "admin", "isGangGe": True},
        {"type": "broker", "id": "ZYJ022"},
        "个人业务档案",
        "manager_to_broker",
        (
            "我语音里可能把名字识别错。",
            "你要知道我说的是 SuFen。",
            "不要把错字写进记忆。",
            "也不要把站小伴的名字混进来。",
            "先按标准名字处理。",
        ),
    ),
    DialogueCase(
        "owner_emotion",
        {"userId": "ZYJ023", "name": "经纪人癸", "role": "broker"},
        {"type": "property", "id": "P-012"},
        "房源维护",
        "broker_to_owner_archive",
        (
            "业主今天态度突然变了。",
            "价格没变，但语气比之前急。",
            "你先看沟通历史。",
            "价格只是一个因素。",
            "我想判断他是不是真的急。",
        ),
    ),
    DialogueCase(
        "client_intent_unclear",
        {"userId": "ZYJ024", "name": "经纪人子", "role": "broker"},
        {"type": "client", "id": "C-011"},
        "客源维护",
        "broker_to_client_archive",
        (
            "这个客户你怎么看？",
            "我也说不清哪里不对。",
            "你可以先问我一个关键问题。",
            "不要一下把所有资料倒出来。",
            "我回你以后再深入。",
        ),
    ),
    DialogueCase(
        "strong_broker_brief",
        {"userId": "ZYJ025", "name": "经纪人丑", "role": "broker"},
        {"type": "broker", "id": "ZYJ025"},
        "个人业务档案",
        "self_to_self",
        (
            "我不需要安慰。",
            "你直接说我下半年最大的风险。",
            "再说一个突破点。",
            "别讲流程。",
            "我自己能执行。",
        ),
    ),
    DialogueCase(
        "new_broker_patient",
        {"userId": "ZYJ026", "name": "经纪人寅", "role": "broker"},
        {"type": "broker", "id": "ZYJ026"},
        "个人业务档案",
        "self_to_self",
        (
            "我还是不懂房源维护怎么做。",
            "我名下房源不少。",
            "但业主不降价。",
            "你别一下讲太复杂。",
            "先告诉我第一步做什么。",
        ),
    ),
    DialogueCase(
        "manager_does_not_need_report",
        {"userId": "ZYJ005", "name": "店长甲", "role": "manager"},
        {"type": "property", "id": "P-013"},
        "房源维护",
        "manager_to_property_archive",
        (
            "这个房子我只想知道值不值得继续盯。",
            "不是让你出完整房源分析报告。",
            "先讲结论。",
            "再讲一个关键依据。",
            "最后讲下一步动作。",
        ),
    ),
    DialogueCase(
        "handoff_next_turn",
        {"userId": "ZYJ027", "name": "经纪人卯", "role": "broker"},
        {"type": "after-sale", "id": "S-011"},
        "售后维护",
        "broker_to_after_sale",
        (
            "我先问一句。",
            "这个售后客户是不是还有维护价值？",
            "你别一次说完所有可能性。",
            "先给我一个判断口子。",
            "我认可的话再展开。",
        ),
    ),
)


REQUIRED_POLICY_NEEDLES = (
    "中文名：素分",
    "会聊天的业务军师",
    "识别场景 -> 判断关系 -> 判断真实意图 -> 选择必要资料",
    "用户原话永远是本轮意图最高优先级",
    "简短问候、在吗、晚安、收到、谢谢",
    "角色感、空间感、时间感",
    "当前操作者可能是刚哥、店长、经纪人本人",
    "操作者和档案对象相同时，不得像向第三方汇报这个人",
    "按北京时间 `Asia/Shanghai`",
    "时间只是线索，不是结论",
    "意图判断和对话节奏",
    "不能连续审问",
    "不做话痨",
    "按需资料选择",
    "不默认全量扫描",
    "最小充分证据",
    "contextLoadPlan",
    "知识图谱",
    "未加载的图谱不得当作已读证据",
    "不得默认读取结算卡",
    "签二手",
    "出房源",
    "签租赁",
    "省心租",
    "签新房",
    "衍生业绩",
    "companyId + operatorUserId + subjectType + subjectId",
)

REQUIRED_PROVIDER_NEEDLES = (
    "本轮 SuFen 执行锚点",
    "currentSufenTime",
    "Asia/Shanghai",
    "回答前先识别操作者",
    "用户本轮原话是最高优先级的意图来源",
    "不得默认全量扫描",
    "不得默认读取结算卡",
    "不做话痨",
    "未标记 loaded 的资料不得假装已读",
    "不得输出“低置信度”口头禅",
)

FORBIDDEN_PROMPT_PATTERNS = (
    "素" + "芬",
    "塑" + "封",
    "塑" + "分",
    r"(?i)\b" + "her" + "mes" + r"\b",
    r"1\s*到\s*2\s*句",
    r"2\s*到\s*4\s*句",
    r"只能.*一句",
)


def _task_for_case(case: DialogueCase) -> SuFenTaskPackage:
    return SuFenTaskPackage.model_validate({
        "operator": case.operator,
        "subject": case.subject,
        "scene": case.scene,
        "archiveContext": {
            "companyId": "company-ZYJ",
            "module": case.scene,
            "subjectRelationHint": case.relation,
            "archiveSummary": "synthetic dialogue policy guard; no production data",
            "contextLoadPlan": {
                "version": "sufen-progressive-context-v1",
                "mode": "progressive",
                "layers": [
                    {"id": "identity_scene", "status": "loaded"},
                    {"id": "current_subject_brief", "status": "loaded"},
                    {"id": "broker_performance", "status": "not_loaded"},
                    {"id": "settlement_cards", "status": "not_loaded"},
                ],
            },
        },
        "scopedMemoryKey": f"company-ZYJ/operators/{case.operator['userId']}/subjects/{case.subject['type']}/{case.subject['id']}",
    })


def _failures_for_needles(text: str, needles: tuple[str, ...], label: str) -> list[str]:
    return [f"{label}: missing {needle!r}" for needle in needles if needle not in text]


def main() -> int:
    failures: list[str] = []
    total_user_turns = sum(len(case.turns) for case in DIALOGUE_CASES)
    if len(DIALOGUE_CASES) < 20:
        failures.append(f"dialogue cases too few: {len(DIALOGUE_CASES)} < 20")
    if total_user_turns < 100:
        failures.append(f"dialogue user turns too few: {total_user_turns} < 100")

    policy_text = build_sufen_identity_block()
    failures.extend(_failures_for_needles(policy_text, REQUIRED_POLICY_NEEDLES, "policy"))

    for pattern in FORBIDDEN_PROMPT_PATTERNS:
        if re.search(pattern, policy_text):
            failures.append(f"policy: forbidden pattern {pattern!r}")

    relation_kinds = {case.relation for case in DIALOGUE_CASES}
    for required_relation in {
        "manager_to_broker",
        "self_to_self",
        "broker_to_owner_archive",
        "broker_to_client_archive",
        "broker_to_after_sale",
    }:
        if required_relation not in relation_kinds:
            failures.append(f"dialogue cases missing relation {required_relation}")

    provider_samples = []
    for case in DIALOGUE_CASES:
        task = _task_for_case(case)
        messages = build_provider_messages("\n".join(case.turns), task)
        provider_samples.append(messages[0]["content"])

    provider_joined = "\n\n".join(provider_samples)
    failures.extend(_failures_for_needles(provider_joined, REQUIRED_PROVIDER_NEEDLES, "provider"))
    for case in DIALOGUE_CASES:
        if case.relation not in provider_joined:
            failures.append(f"provider: missing relation hint {case.relation}")

    if failures:
        print("sufen-dialogue-policy-check failed")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print(json.dumps({
        "ok": True,
        "dialogueCases": len(DIALOGUE_CASES),
        "userTurns": total_user_turns,
        "policyNeedles": len(REQUIRED_POLICY_NEEDLES),
        "providerNeedles": len(REQUIRED_PROVIDER_NEEDLES),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
