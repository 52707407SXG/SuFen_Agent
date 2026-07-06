"""Human terminal UI for the local SuFen CLI."""

from __future__ import annotations

import os
import shutil
import sys
import textwrap
import time

from sufen import __version__
from sufen.auth import extract_authorization_refs
from sufen.config import SuFenSettings
from sufen.output import AuthorizationRequest, EvidenceItem, SuFenResponse, ToolAuditItem


GOLD = "\033[1;38;2;199;160;106m"
PALE = "\033[38;2;244;231;193m"
MUTED = "\033[2m"
RESET = "\033[0m"

SUFEN_MARK = [
    "My Stand",
    " ██████╗ ██╗   ██╗███████╗███████╗███╗   ██╗",
    "██╔════╝ ██║   ██║██╔════╝██╔════╝████╗  ██║",
    "╚█████╗  ██║   ██║█████╗  █████╗  ██╔██╗ ██║",
    " ╚═══██╗ ██║   ██║██╔══╝  ██╔══╝  ██║╚██╗██║",
    "██████╔╝ ╚██████╔╝██║     ███████╗██║ ╚████║",
    "╚═════╝   ╚═════╝ ╚═╝     ╚══════╝╚═╝  ╚═══╝",
]


def _stdout_is_tty() -> bool:
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def _color(text: str, code: str, *, enabled: bool) -> str:
    return f"{code}{text}{RESET}" if enabled else text


def _terminal_width(default: int = 80) -> int:
    return shutil.get_terminal_size((default, 24)).columns


def _center(text: str, width: int) -> str:
    if len(text) >= width:
        return text
    left = max(0, (width - len(text)) // 2)
    return f"{' ' * left}{text}{' ' * (width - len(text) - left)}"


def _animate_startup(*, enabled: bool) -> None:
    if not enabled or os.environ.get("SUFEN_NO_ANIMATION") == "1":
        return
    for frame in ("░░░", "▒▒▒", "▓▓▓", "███"):
        sys.stdout.write(f"\r{GOLD}{frame}{RESET} waking SuFen")
        sys.stdout.flush()
        time.sleep(0.07)
    sys.stdout.write("\r" + " " * 24 + "\r")
    sys.stdout.flush()


def print_startup_card(settings: SuFenSettings) -> None:
    """Print a branded startup card for the human terminal entry."""
    color_enabled = _stdout_is_tty() and os.environ.get("NO_COLOR") != "1"
    _animate_startup(enabled=color_enabled)

    art_width = max(len(line) for line in SUFEN_MARK)
    box_width = min(max(art_width + 4, 62), max(62, _terminal_width()))
    inner = box_width - 2
    model_line = f"{settings.model} · API Usage Billing"
    provider_line = f"provider: {settings.provider} · local archive strategist"

    print(_color("╭" + "─" * inner + "╮", GOLD, enabled=color_enabled))
    for index, line in enumerate(SUFEN_MARK):
        code = GOLD if index == 0 else PALE
        print(_color("│", GOLD, enabled=color_enabled) + _color(_center(line, inner), code, enabled=color_enabled) + _color("│", GOLD, enabled=color_enabled))
    print(_color("├" + "─" * inner + "┤", GOLD, enabled=color_enabled))
    print(_color("│", GOLD, enabled=color_enabled) + _center(f"SuFen v{__version__}", inner) + _color("│", GOLD, enabled=color_enabled))
    print(_color("│", GOLD, enabled=color_enabled) + _center(model_line, inner) + _color("│", GOLD, enabled=color_enabled))
    print(_color("│", GOLD, enabled=color_enabled) + _color(_center(provider_line, inner), MUTED, enabled=color_enabled) + _color("│", GOLD, enabled=color_enabled))
    print(_color("╰" + "─" * inner + "╯", GOLD, enabled=color_enabled))
    print("Welcome back! 直接问，也可以粘贴站内ID；按 Ctrl-D 退出。")


def _compact_query(text: str) -> str:
    return "".join(ch for ch in (text or "").lower() if not ch.isspace())


def terminal_local_response(prompt: str, settings: SuFenSettings) -> SuFenResponse | None:
    """Handle terminal-only prompts that should not hit the strict HTTP contract."""
    query = (prompt or "").strip()
    compact = _compact_query(query)
    if not query:
        return None

    refs = extract_authorization_refs(query)
    identity_hits = (
        "你好",
        "你是谁",
        "介绍",
        "能做什么",
        "help",
        "帮助",
        "怎么用",
        "素分",
        "sufen",
    )
    if any(hit in compact for hit in identity_hits):
        return SuFenResponse(
            answer=(
                "我是 SuFen，My Stand 的档案军师。\n\n"
                "- 我适合陪你拆业主、客户、经纪人和售后档案。\n"
                "- 在 My Stand 页面里打开我时，我会拿到后端注入的授权档案、知识图谱和 scoped memory。\n"
                "- 只在终端裸聊时，我不会越权读生产档案；你可以问通用策略，也可以粘贴站内ID让我识别资料缺口。\n\n"
                f"当前模型：{settings.model} · provider：{settings.provider}。"
            ),
            toolAudit=[
                ToolAuditItem(tool="sufen.terminal", action="local_identity", status="ok")
            ],
        )

    if refs:
        evidence = [
            EvidenceItem(
                source=ref.raw,
                summary=f"识别到 {ref.kind} 资料钥匙；裸终端入口不能绕过 My Stand 后端授权读取正文。",
                confidence=0.8,
            )
            for ref in refs
        ]
        return SuFenResponse(
            answer=(
                "我识别到你给了资料钥匙，但终端裸入口没有 My Stand 后端注入的 taskPackage，"
                "所以我不能直接读正式档案正文。\n\n"
                "正确用法有两个：\n"
                "1. 从 My Stand 对应档案页面打开 SuFen，让后端把当前账号可读资料注入给我。\n"
                "2. 开发验收时用 `sufen chat --task-package <json>` 传入受控任务包。\n\n"
                "我可以先做通用策略讨论，但不会把没有读到的档案当成事实。"
            ),
            evidenceUsed=evidence,
            missingAuthorizationRequests=[
                AuthorizationRequest(
                    reason="missing_task_package",
                    acceptableRefs=["My Stand backend-injected taskPackage"],
                    message="裸终端入口需要 My Stand 后端注入 taskPackage 后才能读取正式档案正文。",
                )
            ],
            toolAudit=[
                ToolAuditItem(tool="sufen.terminal", action="recognize_authorization_refs", status="taskPackage_missing")
            ],
        )

    business_words = ("业主", "客户", "房源", "客源", "售后", "经纪人", "谈", "维护", "降价", "带看", "报价")
    if any(word in query for word in business_words):
        return SuFenResponse(
            answer=(
                "可以先按通用策略拆，但我先说边界：终端裸聊没有当前档案正文，"
                "所以以下只能当低置信度框架，不能当作对某个真实业主/客户的定论。\n\n"
                "建议先拆三件事：\n"
                "1. 事实：对方明确说过什么，哪些只是我们猜的。\n"
                "2. 动机：他真正怕的是价格、时间、面子、家庭意见，还是信息不够。\n"
                "3. 下一步：只给一个低阻力动作，比如补一组对比、约一次复盘、确认一个底线。\n\n"
                "如果要做真档案判断，请从 My Stand 档案页打开 SuFen。"
            ),
            toolAudit=[
                ToolAuditItem(tool="sufen.terminal", action="generic_strategy_without_task", status="low_confidence")
            ],
        )

    return SuFenResponse(
        answer=(
            "我在。终端入口适合做 SuFen 本机调试、通用策略讨论和资料钥匙识别；"
            "真实档案分析要从 My Stand 页面进入，那里会带上账号权限、档案正文、知识图谱和记忆范围。"
        ),
        toolAudit=[ToolAuditItem(tool="sufen.terminal", action="local_chat", status="ok")],
    )


def print_terminal_response(response: SuFenResponse) -> None:
    print()
    for paragraph in response.answer.split("\n"):
        if paragraph:
            print(textwrap.fill(paragraph, width=min(96, max(60, _terminal_width()))))
        else:
            print()

    if response.evidenceUsed:
        print("\n依据:")
        for item in response.evidenceUsed[:5]:
            print(f"- {item.source}: {item.summary}")

    if response.missingAuthorizationRequests:
        print("\n需要资料:")
        for item in response.missingAuthorizationRequests[:3]:
            print(f"- {item.reason}: {item.message}")

    if response.eventDrafts:
        print("\n事件草稿:")
        for item in response.eventDrafts[:3]:
            print(f"- {item.name}: {item.body}")

    if response.fieldPatchDrafts:
        print("\n字段草稿:")
        for item in response.fieldPatchDrafts[:3]:
            print(f"- {item.field}: {item.diff}")

    print()
