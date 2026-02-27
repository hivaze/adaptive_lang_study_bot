"""Centralized StrEnum constants for the adaptive language study bot.

StrEnum members compare equal to their string values, so they are fully
backward-compatible with raw strings in SQL CHECK constraints, Redis keys,
JSON serialization, and any existing code that hasn't been migrated yet.
"""

from enum import StrEnum


class UserTier(StrEnum):
    FREE = "free"
    PREMIUM = "premium"


class SessionType(StrEnum):
    INTERACTIVE = "interactive"
    ONBOARDING = "onboarding"
    PROACTIVE_REVIEW = "proactive_review"
    PROACTIVE_QUIZ = "proactive_quiz"
    PROACTIVE_SUMMARY = "proactive_summary"
    PROACTIVE_NUDGE = "proactive_nudge"
    ASSESSMENT = "assessment"


class PipelineStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class NotificationStatus(StrEnum):
    SENT = "sent"
    SKIPPED_QUIET = "skipped_quiet"
    SKIPPED_PAUSED = "skipped_paused"
    SKIPPED_PREFERENCE = "skipped_preference"
    SKIPPED_LIMIT = "skipped_limit"
    SKIPPED_DEDUP = "skipped_dedup"
    SKIPPED_COOLDOWN = "skipped_cooldown"
    FAILED = "failed"


class Difficulty(StrEnum):
    EASY = "easy"
    NORMAL = "normal"
    HARD = "hard"


class SessionStyle(StrEnum):
    CASUAL = "casual"
    STRUCTURED = "structured"
    INTENSIVE = "intensive"


class ScheduleType(StrEnum):
    DAILY_REVIEW = "daily_review"
    QUIZ = "quiz"
    PROGRESS_REPORT = "progress_report"
    PRACTICE_REMINDER = "practice_reminder"
    CUSTOM = "custom"


class ScheduleStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    EXPIRED = "expired"


class NotificationTier(StrEnum):
    TEMPLATE = "template"
    LLM = "llm"
    HYBRID = "hybrid"


class AccessRequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class CloseReason(StrEnum):
    TURN_LIMIT = "turn_limit"
    COST_LIMIT = "cost_limit"
    SHUTDOWN = "shutdown"
    ERROR = "error"
    IDLE_TIMEOUT = "idle_timeout"
    EXPLICIT_CLOSE = "explicit_close"
    UNKNOWN = "unknown"
