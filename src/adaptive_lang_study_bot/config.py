from dataclasses import dataclass, field

from pydantic_settings import BaseSettings, SettingsConfigDict

from adaptive_lang_study_bot.enums import UserTier  # noqa: F401 — re-exported for backward compat

# Centralized CEFR level progression — imported by tools.py and prompt_builder.py.
CEFR_LEVELS: list[str] = ["A1", "A2", "B1", "B2", "C1", "C2"]


# ---------------------------------------------------------------------------
# Bot tuning constants — centralized so they're easy to find and adjust.
# Previously scattered as magic numbers across tools.py, post_session.py,
# prompt_builder.py, hooks.py, triggers.py, etc.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BotTuning:
    """Tunable behaviour constants for the bot."""

    # -- Difficulty auto-adjust (post_session.py) --
    difficulty_recent_window: int = 5  # how many recent scores to consider (was 7)
    difficulty_up_normal_hard: float = 8.0  # avg to go normal→hard (was 9.0)
    difficulty_up_easy_normal: float = 7.5  # avg to go easy→normal (was 8.5)
    difficulty_down_hard_normal: float = 4.5  # avg to go hard→normal (was 3.0)
    difficulty_down_normal_easy: float = 4.5  # avg to go normal→easy (was 4.0)

    # -- Level auto-adjust (tools.py record_exercise_result) --
    level_up_avg: float = 8.5
    level_down_avg: float = 3.0
    level_recent_window: int = 10

    # -- Weak / strong area thresholds (tools.py) --
    weak_area_score: int = 5  # score threshold to consider "weak" (was 4)
    weak_area_min_occurrences: int = 2  # require N low scores before marking weak
    strong_area_score: int = 7  # score threshold to consider "strong" (was 8)
    strong_area_min_occurrences: int = 3  # require N high scores before marking strong

    # -- User field caps (tools.py update_preference, post_session.py) --
    max_interests: int = 8  # was 5
    max_learning_goals: int = 5  # was 3
    max_topics_to_avoid: int = 5
    max_additional_notes: int = 10

    # -- Streak --
    streak_grace_days: int = 2  # days before streak resets (was 1 day = instant reset)
    streak_decay_threshold: int = 30  # streaks >= this use decay instead of hard reset
    streak_decay_factor: float = 0.7  # keep 70% of streak on reset

    # -- Session history --
    session_history_cap: int = 10

    # -- Notification lookback --
    notification_lookback_hours: int = 12  # was 2

    # -- Comeback adaptation --
    comeback_threshold_hours: float = 48  # was 72
    comeback_vocab_overload_threshold: int = 30  # due_count above this triggers simplified instructions

    # -- Hook hints --
    hook_rolling_avg_window: int = 3
    hook_struggling_threshold: float = 4.0
    hook_excelling_threshold: float = 8.5

    # -- Trigger thresholds (proactive/triggers.py) --
    inactivity_hours_threshold: int = 48
    cards_due_threshold: int = 5
    streak_risk_evening_hour: int = 18
    recent_activity_seconds: int = 7200  # 2 hours
    post_onboarding_min_hours: int = 20
    post_onboarding_24h_max_hours: int = 48
    post_onboarding_3d_max_hours: int = 72
    post_onboarding_7d_max_hours: int = 168
    lapsed_gentle_min_days: int = 2
    lapsed_gentle_max_days: int = 4
    lapsed_compelling_max_days: int = 8
    lapsed_miss_you_max_days: int = 21

    # -- Re-engagement triggers --
    dormant_periodic_max_days: int = 45
    dormant_periodic_interval_days: int = 7
    post_onboarding_max_hours: int = 336  # 14 days (was 168 = 7 days)

    # -- Milestone thresholds --
    milestone_streak: list[int] = field(default_factory=lambda: [3, 7, 10, 30, 50, 100])
    milestone_vocab: list[int] = field(default_factory=lambda: [10, 25, 50, 100, 200, 500])
    milestone_sessions: list[int] = field(default_factory=lambda: [5, 10, 25, 50, 100])
    pending_celebrations_cap: int = 5

    # -- Proactive sessions --
    proactive_session_timeout_seconds: float = 30.0
    proactive_max_turns: int = 10
    proactive_model: str = "claude-haiku-4-5"
    proactive_thinking: str = "disabled"

    # -- Summary sessions --
    summary_session_timeout_seconds: float = 15.0
    summary_max_turns: int = 3
    summary_thinking: str = "adaptive"

    # -- Effort levels --
    interactive_effort: str = "low"
    proactive_effort: str = "low"
    summary_effort: str = "low"

    # -- Proactive tick --
    proactive_dispatch_concurrency: int = 50  # max parallel dispatches per tick phase
    proactive_user_page_size: int = 1000  # users loaded per page in event trigger phase
    proactive_lock_refresh_interval: int = 60  # seconds between lock refreshes during dispatch
    schedule_max_backoff_minutes: int = 1440  # 24h max backoff for failed schedules
    schedule_max_consecutive_failures: int = 10  # auto-pause after this many failures

    # -- Session manager timeouts --
    cleanup_interval_seconds: int = 30
    sdk_close_timeout_seconds: float = 10.0
    db_close_timeout_seconds: float = 5.0
    post_session_timeout_seconds: float = 60.0
    idle_warn_fraction: float = 0.7  # fraction of idle timeout before warning (e.g. 0.7 = 70%)

    # -- Schedule validation --
    min_schedule_interval_minutes: int = 60  # minimum RRULE recurrence interval

    # -- Learning plan --
    plan_min_weeks: int = 2
    plan_max_weeks: int = 8
    plan_default_weeks: int = 4
    plan_max_topics_per_week: int = 8
    plan_topic_min_exercises: int = 3
    plan_topic_mastery_score: float = 7.0
    plan_behind_schedule_pct: float = 20.0
    plan_min_sessions_before_suggest: int = 2

    # -- Health check thresholds (proactive/admin_reports.py) --
    pool_usage_alert_pct: int = 80
    pipeline_failure_threshold: int = 3
    notif_failure_min_total: int = 5
    notif_failure_rate_threshold: float = 0.30


# Singleton — import this everywhere instead of using hardcoded magic numbers.
tuning = BotTuning()


@dataclass(frozen=True)
class TierLimits:
    model: str
    max_turns_per_session: int
    max_sessions_per_day: int
    max_cost_per_day_usd: float
    session_idle_timeout_seconds: int
    thinking_type: str  # "disabled" or "adaptive"
    max_llm_notifications_per_day: int
    rate_limit_per_minute: int
    max_cost_per_session_usd: float
    redis_session_ttl_seconds: int


TIER_LIMITS: dict[UserTier, TierLimits] = {
    UserTier.FREE: TierLimits(
        model="claude-haiku-4-5",
        max_turns_per_session=20,
        max_sessions_per_day=5,
        max_cost_per_day_usd=2.00,
        session_idle_timeout_seconds=360,
        thinking_type="adaptive",
        max_llm_notifications_per_day=2,
        rate_limit_per_minute=5,
        max_cost_per_session_usd=0.40,
        redis_session_ttl_seconds=480,  # idle_timeout (360) + 120s buffer for cleanup loop delays
    ),
    UserTier.PREMIUM: TierLimits(
        model="claude-sonnet-4-6",
        max_turns_per_session=35,
        max_sessions_per_day=15,
        max_cost_per_day_usd=8.00,
        session_idle_timeout_seconds=600,
        thinking_type="adaptive",
        max_llm_notifications_per_day=8,
        rate_limit_per_minute=20,
        max_cost_per_session_usd=1.50,
        redis_session_ttl_seconds=720,  # idle_timeout (600) + 120s buffer for cleanup loop delays
    ),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Telegram
    telegram_bot_token: str

    # Anthropic
    anthropic_api_key: str

    # PostgreSQL
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "langbot"
    postgres_password: str = ""
    postgres_db: str = "langbot"

    # PostgreSQL pool — tools now use per-call sessions (not held for session
    # lifetime), so peak usage is transient: concurrent tool calls +
    # middleware/pipeline sessions.  50 + 30 overflow is generous.
    db_pool_size: int = 150
    db_max_overflow: int = 100
    db_pool_recycle: int = 3600  # seconds
    db_pool_timeout: int = 10  # seconds to wait for a connection before raising

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    redis_max_connections: int = 200

    # Session manager
    max_concurrent_interactive_sessions: int = 500
    max_concurrent_proactive_sessions: int = 50

    # Proactive engine
    proactive_tick_interval_seconds: int = 60
    admin_stats_report_interval_hours: int = 12

    # Admin
    admin_host: str = "0.0.0.0"
    admin_port: int = 7860
    admin_api_token: str = ""  # Set to require Gradio login (empty = no auth)
    admin_telegram_ids: list[int] = []  # env: ADMIN_TELEGRAM_IDS='[123,456]'

    # Logging
    log_level: str = "INFO"  # LOG_LEVEL env var; toggled at runtime via /settings

    # Metrics
    metrics_port: int = 9090  # METRICS_PORT env var; Prometheus HTTP server

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def database_url_sync(self) -> str:
        """Sync URL for Alembic migrations."""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


settings = Settings()
