from html import escape as esc

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from adaptive_lang_study_bot.db.models import User
from adaptive_lang_study_bot.utils import is_user_admin

router = Router()

# Module-level set of user IDs with debug mode enabled.
# In-memory only — lost on restart. Acceptable for a dev tool.
_debug_enabled: set[int] = set()


def is_debug_enabled(user_id: int) -> bool:
    """Check if debug mode is enabled for a user."""
    return user_id in _debug_enabled


def format_debug_info(debug: dict) -> str:
    """Format debug info dict as an HTML <pre> block for Telegram."""
    tools = debug.get("tools_called", [])
    tools_display = ", ".join(esc(tool) for tool in tools) if tools else "none"

    lines = [
        "--- Debug Info ---",
        f"Tools:       {tools_display} ({debug.get('tools_count', 0)})",
        f"Msg cost:    ${debug.get('message_cost', 0):.6f}",
        f"Total cost:  ${debug.get('accumulated_cost', 0):.6f}",
        f"Turns:       {debug.get('turn_count', 0)} (remaining: {debug.get('turns_remaining', '?')})",
        f"Tier:        {esc(str(debug.get('tier', '?')))}",
        f"Model:       {esc(str(debug.get('model', '?')))}",
        f"Duration:    {debug.get('session_duration_s', 0)}s",
        f"Active sess: {debug.get('active_sessions_global', '?')}",
    ]
    return "<pre>" + "\n".join(lines) + "</pre>"


@router.message(Command("debug"))
async def cmd_debug(message: Message, user: User) -> None:
    """Toggle debug mode (admin-only)."""
    if not is_user_admin(user):
        await message.answer("This command is for admins only.")
        return

    user_id = user.telegram_id
    if user_id in _debug_enabled:
        _debug_enabled.discard(user_id)
        await message.answer("Debug mode <b>OFF</b>.")
    else:
        _debug_enabled.add(user_id)
        await message.answer(
            "Debug mode <b>ON</b>.\n"
            "You will see debug info after each message.",
        )
