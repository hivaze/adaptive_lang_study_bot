"""Logging configuration module.

Configures loguru as the primary logger with a stdlib bridge.
Exposes ``set_log_level()`` for runtime log level toggling (admin-only).
"""

import logging
import sys

from loguru import logger

_LOG_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss} | {level:<7} | {name}:{function}:{line} | {message}"
)

# Module-level state: tracks the current loguru handler ID and level.
# In-memory only — resets to configured default on restart.
_handler_id: int | None = None
_current_level: str = "INFO"


class _InterceptHandler(logging.Handler):
    """Bridge stdlib logging → loguru so aiogram/APScheduler errors are visible."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno  # type: ignore[assignment]
        logger.opt(depth=6, exception=record.exc_info).log(level, record.getMessage())


def configure_logging(level: str = "INFO") -> None:
    """Set up loguru and the stdlib bridge. Called once at startup."""
    global _handler_id, _current_level

    logger.remove()
    _handler_id = logger.add(sys.stderr, level=level, format=_LOG_FORMAT)
    _current_level = level

    # Route stdlib logging (aiogram, APScheduler, etc.) through loguru
    logging.basicConfig(handlers=[_InterceptHandler()], level=logging.DEBUG, force=True)


def set_log_level(level: str) -> None:
    """Switch the global log level at runtime.

    Removes the current loguru handler and re-adds it with the new level.
    Also adjusts the stdlib bridge root logger level.
    """
    global _handler_id, _current_level

    if _handler_id is not None:
        logger.remove(_handler_id)

    _handler_id = logger.add(sys.stderr, level=level, format=_LOG_FORMAT)
    _current_level = level

    # Update stdlib root logger to match
    stdlib_level = logging.DEBUG if level == "DEBUG" else logging.INFO
    logging.getLogger().setLevel(stdlib_level)

    logger.info("Log level changed to {}", level)


def get_current_level() -> str:
    """Return the current effective log level."""
    return _current_level


def is_debug_logging() -> bool:
    """Return True if global log level is DEBUG."""
    return _current_level == "DEBUG"
