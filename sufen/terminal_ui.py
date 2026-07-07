"""Human terminal UI for the local SuFen CLI."""

from __future__ import annotations

import os
import shutil
import sys
import threading
import textwrap
import time
import unicodedata

from sufen import __version__
from sufen.auth import extract_authorization_refs
from sufen.config import SuFenSettings
from sufen.output import AuthorizationRequest, EvidenceItem, SuFenResponse, ToolAuditItem
from sufen.provider import (
    ProviderError,
    _chat_completions_url,
    _message_from_provider,
    _post_chat_completions,
)

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.styles import Style as PromptStyle
except Exception:  # pragma: no cover - fallback for stripped installs
    PromptSession = None  # type: ignore[assignment]
    FormattedText = None  # type: ignore[assignment]
    PromptStyle = None  # type: ignore[assignment]


GOLD = "\033[1;38;2;199;160;106m"
MUTED = "\033[2m"
RESET = "\033[0m"
TEXT = "\033[38;2;232;226;214m"
ACCENT = "\033[1;38;2;225;171;92m"

SEPARATOR = "  │  "
STARTUP_CARD_MAX_WIDTH = 142
STARTUP_CARD_SIDE_MARGIN = 4
SUFEN_WORDMARK = (
    "██████  ██   ██  ███████ ███████ ███   ██",
    "██      ██   ██  ██      ██      ████  ██",
    "██████  ██   ██  █████   █████   ██ ██ ██",
    "    ██  ██   ██  ██      ██      ██  ████",
    "██████  ██████   ██      ███████ ██   ███",
)
TERMINAL_SYSTEM_PROMPT = (
    "你是 SuFen，中文名固定是“素分”，不要写成“苏芬”。你是 My Stand 的档案军师。当前是在服务器裸终端里的普通聊天入口，"
    "没有 My Stand 后端注入的 taskPackage、正式档案正文、入口指定知识图谱或 SuFen 日志。"
    "请像正常同事一样用中文自然对话，不要像产品说明书，不要输出 JSON。"
    "可以聊判断、话术、下一步打法和通用业务策略。"
    "如果用户给 AUTH、OUT、KGREF、ref_ 或 knowledge: 这类站内资料钥匙，"
    "只能说明需要从 My Stand 对应页面打开 SuFen 才能读取，不能假装已经读到。"
    "回答要短一点，先接住用户，再给有用的下一句。"
)
SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


def _stdout_is_tty() -> bool:
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def _color(text: str, code: str, *, enabled: bool) -> str:
    return f"{code}{text}{RESET}" if enabled else text


def clear_terminal_screen() -> None:
    if _stdout_is_tty() and os.environ.get("SUFEN_NO_CLEAR") != "1":
        print("\033[2J\033[H", end="")


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


def _rule(width: int) -> str:
    return "─" * max(0, width)


def _cell(text: str, width: int, *, align: str = "left") -> str:
    clipped = _clip(text, width)
    if align == "center":
        return _center(clipped, width)
    return clipped + (" " * max(0, width - _display_width(clipped)))


def _format_cwd(width: int) -> str:
    cwd = os.getenv("TERMINAL_CWD", os.getcwd())
    home = os.path.expanduser("~")
    if cwd == home:
        cwd = "~"
    return _clip(cwd, width)


def _status_bar_text(settings: SuFenSettings, width: int | None = None) -> str:
    width = width or _terminal_width()
    model = settings.model.split("/")[-1]
    if width < 52:
        return _clip(f"SuFen {model} · 0s", width)
    if width < 76:
        return _clip(f"SuFen {model} · -- · 0s", width)
    return _clip(f"SuFen {model} │ ctx -- │ [░░░░░░░░░░] -- │ 0s │ ⏲ 0s", width)


def _runtime_status_bar_text(settings: SuFenSettings, frame: str, elapsed: float, width: int | None = None) -> str:
    width = width or _terminal_width()
    model = settings.model.split("/")[-1]
    elapsed_label = f"{elapsed:.1f}s"
    if width < 52:
        return _clip(f"SuFen {model} · runtime {frame} · {elapsed_label}", width)
    if width < 76:
        return _clip(f"SuFen {model} · runtime {frame} · {elapsed_label}", width)
    return _clip(
        f"SuFen {model} │ runtime {frame} │ [░░░░░░░░░░] -- │ 0s │ ⏲ {elapsed_label}",
        width,
    )


def _clear_runtime_line() -> None:
    sys.stdout.write("\r\033[2K")
    sys.stdout.flush()


def _runtime_line(settings: SuFenSettings, frame: str, elapsed: float, status: str = "runtime") -> str:
    return _runtime_status_bar_text(settings, frame, elapsed)


class TerminalRuntimeActivity:
    def __init__(self, settings: SuFenSettings, *, status: str = "runtime") -> None:
        self.settings = settings
        self.status = status
        self.enabled = _stdout_is_tty()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at = 0.0

    def __enter__(self) -> "TerminalRuntimeActivity":
        if not self.enabled:
            return self
        self._started_at = time.monotonic()
        # prompt_toolkit leaves the submitted prompt in scrollback; move the
        # runtime line onto its own row before repainting it in-place.
        sys.stdout.write("\n")
        sys.stdout.flush()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, _exc, _tb) -> None:
        if not self.enabled:
            return None
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)
        elapsed = max(0.0, time.monotonic() - self._started_at)
        symbol = "!" if exc_type else "✓"
        _clear_runtime_line()
        print(_color(_runtime_line(self.settings, symbol, elapsed, self.status), TEXT, enabled=True))
        return None

    def _run(self) -> None:
        index = 0
        while not self._stop.wait(0.35):
            elapsed = max(0.0, time.monotonic() - self._started_at)
            width = max(20, _terminal_width())
            line = _runtime_line(self.settings, SPINNER_FRAMES[index % len(SPINNER_FRAMES)], elapsed, self.status)
            sys.stdout.write("\r\033[2K" + _color(_clip(line, width - 1), TEXT, enabled=True))
            sys.stdout.flush()
            index += 1


def terminal_runtime_activity(settings: SuFenSettings) -> TerminalRuntimeActivity:
    return TerminalRuntimeActivity(settings)


def _message_content_text(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        pieces: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    pieces.append(text)
        return "\n".join(piece.strip() for piece in pieces if piece.strip()).strip()
    return ""


def _normalize_sufen_display_name(text: str) -> str:
    return (text or "").replace("苏芬", "素分").replace("蘇芬", "素分")


def _sufen_wordmark_lines(width: int) -> tuple[str, ...]:
    if max(_display_width(line) for line in SUFEN_WORDMARK) <= width:
        return SUFEN_WORDMARK
    return ("SuFen",)


def _print_welcome_row(
    *,
    left: str,
    right: str = "",
    left_width: int,
    right_width: int,
    color_enabled: bool,
    left_color: str = TEXT,
    right_color: str = TEXT,
    left_align: str = "center",
    right_align: str = "left",
) -> None:
    print(
        _color("│", GOLD, enabled=color_enabled)
        + _color(_cell(left, left_width, align=left_align), left_color, enabled=color_enabled)
        + _color(SEPARATOR, MUTED, enabled=color_enabled)
        + _color(_cell(right, right_width, align=right_align), right_color, enabled=color_enabled)
        + _color("│", GOLD, enabled=color_enabled)
    )


def print_startup_card(settings: SuFenSettings) -> None:
    """Print the My Stand-style startup card for the human terminal entry."""
    clear_terminal_screen()
    color_enabled = _stdout_is_tty() and os.environ.get("NO_COLOR") != "1"
    columns = max(40, _terminal_width())
    available_width = max(40, columns - STARTUP_CARD_SIDE_MARGIN)
    frame_width = min(STARTUP_CARD_MAX_WIDTH, max(64, available_width), max(40, columns - 2))
    inner_width = frame_width - 2
    separator_width = _display_width(SEPARATOR)
    left_width = min(56, max(24, inner_width // 2 - 3))
    right_width = inner_width - left_width - separator_width
    if right_width < 16:
        right_width = max(10, min(16, inner_width - separator_width - 10))
        left_width = max(10, inner_width - separator_width - right_width)
    model = settings.model.split("/")[-1]
    title = f"SuFen v{__version__}"
    top_rule = max(0, frame_width - _display_width(title) - 5)
    wordmark_lines = _sufen_wordmark_lines(left_width)

    print(
        _color("╭─ ", GOLD, enabled=color_enabled)
        + _color(title, ACCENT, enabled=color_enabled)
        + _color(" " + ("─" * top_rule) + "╮", GOLD, enabled=color_enabled)
    )
    _print_welcome_row(
        left="Welcome back!",
        right="Tips for getting started",
        left_width=left_width,
        right_width=right_width,
        color_enabled=color_enabled,
        right_color=ACCENT,
    )
    _print_welcome_row(
        left="My Stand SuFen Agent",
        right="Run /help to see SuFen commands",
        left_width=left_width,
        right_width=right_width,
        color_enabled=color_enabled,
    )
    _print_welcome_row(
        left="",
        right="Run /new to start a clean session",
        left_width=left_width,
        right_width=right_width,
        color_enabled=color_enabled,
    )
    for index, logo_line in enumerate(wordmark_lines):
        right = ""
        right_color = TEXT
        if index == 0:
            right = _rule(min(64, right_width))
            right_color = MUTED
        elif index == 1:
            right = "Recent activity"
            right_color = ACCENT
        elif index == 2:
            right = "No recent activity"
            right_color = MUTED
        _print_welcome_row(
            left=logo_line,
            right=right,
            left_width=left_width,
            right_width=right_width,
            color_enabled=color_enabled,
            left_color=ACCENT,
            right_color=right_color,
        )
    if len(wordmark_lines) < 2:
        _print_welcome_row(
            left="",
            right="Recent activity",
            left_width=left_width,
            right_width=right_width,
            color_enabled=color_enabled,
            right_color=ACCENT,
        )
        _print_welcome_row(
            left="",
            right="No recent activity",
            left_width=left_width,
            right_width=right_width,
            color_enabled=color_enabled,
            right_color=MUTED,
        )
    _print_welcome_row(
        left="",
        right="",
        left_width=left_width,
        right_width=right_width,
        color_enabled=color_enabled,
    )
    _print_welcome_row(
        left=f"{model} · API Usage Billing",
        right="",
        left_width=left_width,
        right_width=right_width,
        color_enabled=color_enabled,
    )
    _print_welcome_row(
        left=_format_cwd(left_width),
        right="",
        left_width=left_width,
        right_width=right_width,
        color_enabled=color_enabled,
        left_color=MUTED,
    )
    print(_color("╰" + ("─" * inner_width) + "╯", GOLD, enabled=color_enabled))
    print()


def print_terminal_intro(settings: SuFenSettings) -> None:
    color_enabled = _stdout_is_tty() and os.environ.get("NO_COLOR") != "1"
    columns = max(60, _terminal_width())
    status_width = min(columns - 1, 150)
    print("Welcome to SuFen! Type your message or /help for commands.")
    print(_color("✦ Tip: 从 My Stand 档案页打开 SuFen，才能带入正式授权资料。", MUTED, enabled=color_enabled))
    print()
    print(_color(_status_bar_text(settings, status_width), TEXT, enabled=color_enabled))
    print(_color(_rule(status_width), MUTED, enabled=color_enabled))


def terminal_prompt() -> str:
    return "❯ "


def _can_use_prompt_toolkit() -> bool:
    if PromptSession is None or PromptStyle is None or FormattedText is None:
        return False
    if not _stdout_is_tty():
        return False
    fileno = getattr(sys.stdin, "fileno", None)
    if fileno is None:
        return False
    try:
        fileno()
    except Exception:
        return False
    return True


def _disable_prompt_toolkit_cpr_warning(session: object) -> None:
    try:
        session.app.renderer.cpr_not_supported_callback = None
    except Exception:
        pass


class TerminalPromptSession:
    def __init__(self, settings: SuFenSettings) -> None:
        self.settings = settings
        self._session = None
        if _can_use_prompt_toolkit():
            style = PromptStyle.from_dict({
                "prompt": "#ffffff bold",
                "bottom-toolbar": "bg:#1d1f21 #e8e2d6",
            })
            self._session = PromptSession(
                message=FormattedText([("class:prompt", terminal_prompt())]),
                bottom_toolbar=self._bottom_toolbar,
                refresh_interval=0.5,
                style=style,
            )
            _disable_prompt_toolkit_cpr_warning(self._session)

    def prompt(self) -> str:
        if self._session is None:
            # Fallback only: real TTY Chinese input uses prompt_toolkit to avoid CPR/Unicode terminal issues.
            return input(terminal_prompt())
        return self._session.prompt()

    def _bottom_toolbar(self):
        return FormattedText([("class:bottom-toolbar", " " + _status_bar_text(self.settings))])


def make_terminal_prompt_session(settings: SuFenSettings) -> TerminalPromptSession:
    return TerminalPromptSession(settings)


def _compact_query(text: str) -> str:
    return "".join(ch for ch in (text or "").lower() if not ch.isspace())


def trim_terminal_history(history: list[dict[str, str]], *, max_messages: int = 12, max_chars: int = 6000) -> list[dict[str, str]]:
    """Keep bare-terminal chat history short enough for a CLI session."""
    kept = list(history[-max_messages:])
    while kept and sum(len(item.get("content", "")) for item in kept) > max_chars:
        kept.pop(0)
    return kept


def terminal_provider_response(
    prompt: str,
    settings: SuFenSettings,
    history: list[dict[str, str]] | None = None,
) -> SuFenResponse:
    """Answer a bare-terminal prompt through the real provider without task tools."""
    if not settings.provider_api_key.strip():
        raise ProviderError("missing_sufen_provider_api_key")
    headers = {
        "Authorization": f"Bearer {settings.provider_api_key}",
        "Content-Type": "application/json",
    }
    messages = [
        {"role": "system", "content": TERMINAL_SYSTEM_PROMPT},
        *trim_terminal_history(history or []),
        {"role": "user", "content": prompt},
    ]
    payload = {
        "model": settings.model,
        "messages": messages,
        "temperature": 0.5,
        "max_tokens": 700,
    }
    data = _post_chat_completions(_chat_completions_url(settings), headers, payload)
    message = _message_from_provider(data)
    answer = _message_content_text(message.get("content"))
    if not answer:
        raise ProviderError("provider terminal response was empty")
    answer = _normalize_sufen_display_name(answer)
    return SuFenResponse(
        answer=answer,
        toolAudit=[
            ToolAuditItem(
                tool="provider.chat_completions",
                action="terminal_chat",
                status=f"ok:{settings.provider}:{settings.model}",
            )
        ],
    )


def terminal_fallback_response(prompt: str, settings: SuFenSettings, reason: str) -> SuFenResponse:
    """Natural local fallback when the terminal provider is unavailable."""
    business_words = ("业主", "客户", "房源", "客源", "售后", "经纪人", "谈", "维护", "降价", "带看", "报价")
    if any(word in prompt for word in business_words):
        answer = (
            "可以，先按你现在给的信息拆。\n\n"
            "我会先看：对方明确说了什么、真正卡住他的点是什么、下一步能不能做成一个低压力动作。"
            "你把具体情况发我，我先按通用经验帮你捋。"
        )
    else:
        answer = (
            "我在。你直接说事就行。\n\n"
            "现在真实模型没接上，我先用本机兜底回你；要聊具体档案，还是从 My Stand 页面打开 SuFen 更准。"
        )
    return SuFenResponse(
        answer=answer,
        toolAudit=[
            ToolAuditItem(tool="sufen.terminal", action="local_fallback", status=reason[:500])
        ],
    )


def terminal_local_response(prompt: str, settings: SuFenSettings) -> SuFenResponse | None:
    """Handle terminal-only prompts that must not hit the provider."""
    query = (prompt or "").strip()
    if not query:
        return None

    refs = extract_authorization_refs(query)
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
                "这个站内 ID 我认出来了，但现在是在裸终端里，后端没有把对应档案正文和权限包带过来，"
                "所以我不能直接当成已经读到资料。\n\n"
                "你要我真看这份档案，就从 My Stand 对应页面打开 SuFen；"
                "如果只是先讨论打法，可以直接把你知道的情况发我，我按你给的信息帮你拆。"
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
    return None


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
