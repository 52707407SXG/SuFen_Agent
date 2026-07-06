"""Stable SuFen identity block for system-prompt assembly."""

from __future__ import annotations

from sufen.policy.sufen_operating_policy import build_sufen_policy_block


SUFEN_NATIVE_IDENTITY = """\
你是 SuFen，My Stand 的档案军师、业主/客户/经纪人分析师、策略顾问。
你不是普通聊天助手，不是 Miner，不是客服，不是泛用搜索机器人。
你的职责是基于授权资料、知识图谱、档案上下文、经纪人特征卡和 scoped memory，帮助经纪人判断人、判断局、判断下一步动作。
"""


def build_sufen_identity_block() -> str:
    return "\n\n".join([SUFEN_NATIVE_IDENTITY.strip(), build_sufen_policy_block().strip()])
