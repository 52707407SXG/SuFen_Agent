"""Human terminal UI for the local SuFen CLI."""

from __future__ import annotations

import os
import shutil
import sys
import textwrap
import unicodedata

from sufen import __version__
from sufen.auth import extract_authorization_refs
from sufen.config import SuFenSettings
from sufen.output import AuthorizationRequest, EvidenceItem, SuFenResponse, ToolAuditItem


GOLD = "\033[1;38;2;199;160;106m"
MUTED = "\033[2m"
RESET = "\033[0m"
TEXT = "\033[38;2;232;226;214m"
ACCENT = "\033[1;38;2;225;171;92m"

TAGLINE = "素分 SuFen · My Stand 档案军师"


def _stdout_is_tty() -> bool:
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def _color(text: str, code: str, *, enabled: bool) -> str:
    return f"{code}{text}{RESET}" if enabled else text


def _terminal_width(default: int = 80) -> int:
    return shutil.get_terminal_size((default, 24)).columns


def _char_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1


def _display_width(text: str) -> int:
    return sum(_char_width(char) for char in text)


def _center(text: str, width: int) -> str:
    text_width = _display_width(text)
    if text_width >= width:
        return text
    left = max(0, (width - text_width) // 2)
    return f"{' ' * left}{text}{' ' * (width - text_width - left)}"


def _clip(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if _display_width(text) <= width:
        return text
    suffix = "…"
    target = max(0, width - _display_width(suffix))
    pieces: list[str] = []
    used = 0
    for char in text:
        char_width = _char_width(char)
        if used + char_width > target:
            break
        pieces.append(char)
        used += char_width
    return "".join(pieces).rstrip() + suffix


def _plain_cell(text: str, width: int, *, color: str = TEXT, enabled: bool) -> str:
    clipped = _clip(text, width)
    padded = clipped + (" " * max(0, width - _display_width(clipped)))
    return _color(padded, color, enabled=enabled)


def _center_cell(text: str, width: int, *, color: str = TEXT, enabled: bool) -> str:
    return _color(_center(_clip(text, width), width), color, enabled=enabled)


def _rule(width: int) -> str:
    return "─" * max(0, width)


def _print_box_line(text: str, body: int, *, color: str, enabled: bool, margin: str) -> None:
    print(
        margin
        + _color("│", GOLD, enabled=enabled)
        + _plain_cell(text, body, color=color, enabled=enabled)
        + _color("│", GOLD, enabled=enabled)
    )


def _print_box_center(text: str, body: int, *, color: str, enabled: bool, margin: str) -> None:
    print(
        margin
        + _color("│", GOLD, enabled=enabled)
        + _center_cell(text, body, color=color, enabled=enabled)
        + _color("│", GOLD, enabled=enabled)
    )


def print_startup_card(settings: SuFenSettings) -> None:
    """Print a branded startup card for the human terminal entry."""
    color_enabled = _stdout_is_tty() and os.environ.get("NO_COLOR") != "1"
    columns = max(56, _terminal_width())
    frame_width = min(max(64, columns - 2), 96)
    body = frame_width - 2
    margin = " " * max(0, (columns - frame_width) // 2)

    title = f"SuFen v{__version__}"
    top_rule = max(0, body - _display_width(title) - 2)
    print(
        margin
        + _color("╭─ ", GOLD, enabled=color_enabled)
        + _color(title, ACCENT, enabled=color_enabled)
        + _color(" " + ("─" * top_rule) + "╮", GOLD, enabled=color_enabled)
    )
    _print_box_center("SuFen", body, color=ACCENT, enabled=color_enabled, margin=margin)
    _print_box_line(TAGLINE, body, color=GOLD, enabled=color_enabled, margin=margin)
    _print_box_line("终端入口：通用策略讨论 / 站内ID识别 / 本机调试", body, color=TEXT, enabled=color_enabled, margin=margin)
    _print_box_line("正式档案：从 My Stand 档案页打开，由后端注入授权资料", body, color=TEXT, enabled=color_enabled, margin=margin)
    print(margin + _color("├" + ("─" * body) + "┤", GOLD, enabled=color_enabled))
    _print_box_line(f"模型：{settings.model} · provider：{settings.provider}", body, color=ACCENT, enabled=color_enabled, margin=margin)
    _print_box_line("上下文：terminal · 未注入 taskPackage · 不读生产档案", body, color=MUTED, enabled=color_enabled, margin=margin)
    print(margin + _color("╰" + ("─" * body) + "╯", GOLD, enabled=color_enabled))
    print()


def print_terminal_intro(settings: SuFenSettings) -> None:
    color_enabled = _stdout_is_tty() and os.environ.get("NO_COLOR") != "1"
    columns = max(64, _terminal_width())
    status_width = min(max(64, columns - 2), 96)
    margin = " " * max(0, (columns - status_width) // 2)
    model = settings.model.split("/")[-1]
    print("素分本机入口已就绪。输入 /help 查看命令，/new 新会话，/quit 退出。")
    print("提示：终端入口不读取生产档案；从 My Stand 档案页打开才会带入授权资料。")
    status = (
        _color("SuFen", GOLD, enabled=color_enabled)
        + _color(" │ ", GOLD, enabled=color_enabled)
        + _color("terminal", TEXT, enabled=color_enabled)
        + _color(" │ ", GOLD, enabled=color_enabled)
        + _color(model, TEXT, enabled=color_enabled)
        + _color(" │ ", GOLD, enabled=color_enabled)
        + _color("context: none", MUTED, enabled=color_enabled)
    )
    print(margin + status)


def terminal_prompt() -> str:
    return "sufen> "


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
