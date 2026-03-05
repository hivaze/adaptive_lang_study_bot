"""Shared bot helpers used across routers (start, settings, etc.)."""

import re
import uuid

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger

from adaptive_lang_study_bot.db.models import User
from adaptive_lang_study_bot.i18n import DEFAULT_LANGUAGE, t

# Telegram enforces a 4096-character limit on message text.
TELEGRAM_MSG_MAX_LEN = 4096

# ---------------------------------------------------------------------------
# Markdown → Telegram HTML converter
# ---------------------------------------------------------------------------
# The LLM naturally outputs GitHub-Flavored Markdown. Since Telegram uses
# HTML parse mode, we convert the common GFM patterns to their Telegram HTML
# equivalents.  The converter protects code blocks/inline code first (via
# placeholder tokens) to avoid processing their contents.

_CODE_BLOCK_RE = re.compile(r"```\w*\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_ITALIC_RE = re.compile(r"\*\*\*(.+?)\*\*\*", re.DOTALL)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_HEADER_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_HR_RE = re.compile(r"^-{3,}\s*$", re.MULTILINE)
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
# Matches a contiguous block of markdown table lines (header + separator + data rows).
_TABLE_RE = re.compile(
    r"(?:^[ \t]*\|.+\|[ \t]*\n)+"
    r"(?:^[ \t]*\|[-| :]+\|[ \t]*\n)"
    r"(?:^[ \t]*\|.+\|[ \t]*\n)*",
    re.MULTILINE,
)
# Matches the separator row (e.g. |---|---|) used to detect and strip it.
_TABLE_SEP_RE = re.compile(r"^[ \t]*\|[-| :]+\|[ \t]*$")
# Backslash escapes: \* \_ \| \` \[ \] \( \) \# \~ \> \! \- \+ \.
_BACKSLASH_ESCAPE_RE = re.compile(r"\\([*_|`\[\]()#~>!\-+.])")

# Matches HTML open/close tags (b, i, u, s, a, code, pre).
_HTML_TAG_RE = re.compile(r"<(/?)([a-z]+)(?:\s[^>]*)?>")
# Matches empty tag pairs like <i></i>, <b></b> left after nesting fixes.
_EMPTY_TAG_RE = re.compile(r"<([a-z]+)(?:\s[^>]*)?>(\s*)</\1>")


def _fix_tag_nesting(html: str) -> str:
    """Fix overlapping HTML tags to ensure proper nesting for Telegram.

    Telegram's HTML parser rejects overlapping tags like ``<b>...<i>...</b></i>``
    and the message falls back to plaintext, exposing raw tags to the user.

    This uses a stack-based approach: when a closing tag doesn't match the
    top of the stack, intermediate tags are closed and reopened to maintain
    valid nesting.
    """
    result: list[str] = []
    # Stack entries: (tag_name, full_opening_tag_string)
    stack: list[tuple[str, str]] = []
    last_end = 0

    for m in _HTML_TAG_RE.finditer(html):
        # Append text between tags
        if m.start() > last_end:
            result.append(html[last_end:m.start()])
        last_end = m.end()

        is_close = m.group(1) == "/"
        tag_name = m.group(2)
        full_tag = m.group(0)

        if not is_close:
            result.append(full_tag)
            stack.append((tag_name, full_tag))
        else:
            # Find the matching open tag in the stack
            match_idx = None
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == tag_name:
                    match_idx = i
                    break

            if match_idx is None:
                # Stray closing tag with no matching opener — drop it
                continue

            # Close all tags from top of stack down to (and including) the match
            tags_to_reopen: list[tuple[str, str]] = []
            while len(stack) > match_idx:
                popped_name, popped_open = stack.pop()
                result.append(f"</{popped_name}>")
                if popped_name != tag_name:
                    tags_to_reopen.append((popped_name, popped_open))

            # Reopen the intermediate tags that were closed early
            for name, open_tag in reversed(tags_to_reopen):
                result.append(open_tag)
                stack.append((name, open_tag))

    # Append remaining text after last tag
    if last_end < len(html):
        result.append(html[last_end:])

    # Close any tags still open at the end
    while stack:
        result.append(f"</{stack.pop()[0]}>")

    # Strip empty tag pairs like <i></i> left by reopening + immediate close
    out = "".join(result)
    while _EMPTY_TAG_RE.search(out):
        out = _EMPTY_TAG_RE.sub(r"\2", out)
    return out


def _convert_table(match: re.Match) -> str:
    """Convert a markdown table to a <pre> block preserving column alignment."""
    lines = match.group(0).strip().splitlines()
    # Parse rows, skip separator line
    rows: list[list[str]] = []
    for line in lines:
        if _TABLE_SEP_RE.match(line):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        rows.append(cells)
    if not rows:
        return match.group(0)
    # Compute column widths
    n_cols = max(len(r) for r in rows)
    col_widths = [0] * n_cols
    for row in rows:
        for i, cell in enumerate(row):
            if i < n_cols:
                col_widths[i] = max(col_widths[i], len(cell))
    # Format rows with padding
    formatted: list[str] = []
    for row in rows:
        parts = []
        for i in range(n_cols):
            cell = row[i] if i < len(row) else ""
            parts.append(cell.ljust(col_widths[i]))
        formatted.append("  ".join(parts).rstrip())
    return "<pre>" + "\n".join(formatted) + "</pre>"


def markdown_to_telegram_html(text: str) -> str:
    """Convert common Markdown formatting to Telegram HTML.

    Handles: code blocks, inline code, bold+italic, bold, italic, headers,
    horizontal rules, links, tables, and backslash escapes.  Operates in a
    safe order — code spans and tables are replaced with placeholders first
    so their contents are not mangled.
    """
    placeholders: dict[str, str] = {}

    def _protect(match: re.Match, tag: str) -> str:
        token = f"\x00PH{uuid.uuid4().hex}\x00"
        inner = match.group(1)
        placeholders[token] = f"<{tag}>{inner}</{tag}>"
        return token

    def _protect_raw(html: str) -> str:
        token = f"\x00PH{uuid.uuid4().hex}\x00"
        placeholders[token] = html
        return token

    # 1. Protect code blocks (``` ... ```)
    text = _CODE_BLOCK_RE.sub(lambda m: _protect(m, "pre"), text)
    # 2. Protect inline code (` ... `)
    text = _INLINE_CODE_RE.sub(lambda m: _protect(m, "code"), text)
    # 3. Convert markdown tables to <pre> and protect them
    text = _TABLE_RE.sub(lambda m: _protect_raw(_convert_table(m)), text)
    # 4. Strip backslash escapes and protect the literal characters
    def _protect_escape(m: re.Match) -> str:
        token = f"\x00PH{uuid.uuid4().hex}\x00"
        placeholders[token] = m.group(1)
        return token

    text = _BACKSLASH_ESCAPE_RE.sub(_protect_escape, text)
    # 5. Bold+italic (***text***) — must come before bold/italic
    text = _BOLD_ITALIC_RE.sub(r"<b><i>\1</i></b>", text)
    # 6. Bold (**text**)
    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    # 7. Italic (*text*) — only after bold is consumed
    text = _ITALIC_RE.sub(r"<i>\1</i>", text)
    # 8. Headers (## text → bold)
    text = _HEADER_RE.sub(r"<b>\1</b>", text)
    # 9. Horizontal rules (--- → remove)
    text = _HR_RE.sub("", text)
    # 10. Links [text](url)
    text = _LINK_RE.sub(r'<a href="\2">\1</a>', text)

    # Restore protected code spans and tables
    for token, replacement in placeholders.items():
        text = text.replace(token, replacement)

    # Fix any overlapping tags produced by regex edge cases
    text = _fix_tag_nesting(text)

    return text


# Maps raw DB enum values to their i18n keys for display.
_DISPLAY_VALUE_KEYS: dict[str, str] = {
    # preferred_difficulty
    "easy": "settings.btn_easy",
    "normal": "settings.btn_normal",
    "hard": "settings.btn_hard",
    # session_style
    "casual": "settings.btn_casual",
    "structured": "settings.btn_structured",
    "intensive": "settings.btn_intensive",
    # user tier
    "free": "stats.tier_free",
    "premium": "stats.tier_premium",
}

# Maps DB field names to their i18n keys for display.
_DISPLAY_FIELD_KEYS: dict[str, str] = {
    "preferred_difficulty": "settings.field_preferred_difficulty",
    "session_style": "settings.field_session_style",
}


def localize_value(value: str, lang: str) -> str:
    """Translate a raw enum value (e.g. "normal") to the user's language."""
    key = _DISPLAY_VALUE_KEYS.get(value)
    if key:
        return t(key, lang)
    return value


def localize_field_name(field: str, lang: str) -> str:
    """Translate a DB field name (e.g. "preferred_difficulty") to the user's language."""
    key = _DISPLAY_FIELD_KEYS.get(field)
    if key:
        return t(key, lang)
    return field.replace("_", " ")


# Regex to split agent output on a --- line (with optional horizontal whitespace).
# Uses [ \t]* instead of \s* so newlines are not consumed greedily.
_SECTION_SPLIT_RE = re.compile(r"\n[ \t]*---[ \t]*\n")


def split_agent_sections(text: str) -> list[str]:
    """Split agent output on ``---`` line delimiters into separate message sections.

    The agent is instructed to place ``---`` on its own line between logically
    distinct sections (greeting, exercise, feedback, etc.).  Each section is
    sent as a separate Telegram message.

    IMPORTANT: Call this on raw Markdown text *before* ``markdown_to_telegram_html``
    because the HTML converter removes ``---`` (horizontal rules).
    """
    parts = _SECTION_SPLIT_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


async def send_html_safe(
    send_fn,
    text: str,
    *,
    user_id: int = 0,
) -> bool:
    """Send a message with HTML parse mode, falling back to plaintext on parse error.

    ``send_fn`` must be a coroutine accepting ``(text, **kwargs)`` — e.g.
    ``message.answer`` or ``bot.send_message``.  Returns True if the message
    was sent (in either mode).
    """
    try:
        await send_fn(text)
        return True
    except TelegramBadRequest:
        logger.debug("HTML parse failed for user {}, falling back to plaintext", user_id)
        try:
            await send_fn(text, parse_mode=None)
            return True
        except Exception:
            logger.exception("Failed to send message to user {}", user_id)
            return False


def get_user_lang(user: User) -> str:
    """Resolve user's UI language with fallback."""
    return user.native_language or DEFAULT_LANGUAGE


async def safe_edit_text(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    lang: str = DEFAULT_LANGUAGE,
) -> None:
    """Edit callback message, silently handling stale/deleted messages."""
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        logger.debug("edit_text failed for callback ({}): {}", callback.data, e)
        try:
            await callback.answer(t("settings.msg_outdated", lang), show_alert=True)
        except Exception:
            pass


def build_filterable_keyboard(
    items: list[tuple[str, str]],
    *,
    popular: frozenset[str] | None = None,
    show_all: bool = False,
    prefix: str,
    more_callback: str | None = None,
    more_label: str = "",
    back_callback: str | None = None,
    back_label: str = "",
    text_override: dict[str, str] | None = None,
) -> InlineKeyboardMarkup:
    """Build a filterable inline keyboard from (code, label) items.

    Args:
        items: Full list of (code, label) pairs.
        popular: If provided and *show_all* is False, only show items in this set.
        show_all: Show all items regardless of *popular*.
        prefix: Callback data prefix (e.g. ``"tz"`` produces ``"tz:UTC"``).
        more_callback: If set, add a "show more" button when *show_all* is False.
        more_label: Text for the "show more" button.
        back_callback: If set, append a "back" button row.
        back_label: Text for the "back" button.
        text_override: Map of code -> display text to override for specific items.
    """
    visible = items if show_all or popular is None else [
        (c, l) for c, l in items if c in popular
    ]
    overrides = text_override or {}
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            text=overrides.get(code, label),
            callback_data=f"{prefix}:{code}",
        )]
        for code, label in visible
    ]
    if not show_all and more_callback and more_label:
        rows.append([InlineKeyboardButton(text=more_label, callback_data=more_callback)])
    if back_callback and back_label:
        rows.append([InlineKeyboardButton(text=back_label, callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def safe_edit_markup(
    callback: CallbackQuery,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Edit callback reply markup, silently handling stale/deleted messages."""
    if callback.message is None:
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=reply_markup)
    except TelegramBadRequest:
        logger.debug("edit_reply_markup failed for callback ({})", callback.data)
