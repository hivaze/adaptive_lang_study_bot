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
    difficulty_recent_window: int = 5  # how many recent scores to consider
    difficulty_up_normal_hard: float = 8.0  # avg to go normal→hard
    difficulty_up_easy_normal: float = 7.5  # avg to go easy→normal
    difficulty_down_hard_normal: float = 4.5  # avg to go hard→normal
    difficulty_down_normal_easy: float = 4.5  # avg to go normal→easy

    # -- Level auto-adjust (tools.py record_exercise_result) --
    level_up_avg: float = 8.5
    level_down_avg: float = 3.0
    level_recent_window: int = 15

    # -- Weak / strong area thresholds (tools.py) --
    weak_area_score: int = 5  # score threshold to consider "weak"
    weak_area_min_occurrences: int = 2  # require N low scores before marking weak
    strong_area_score: int = 7  # score threshold to consider "strong"
    strong_area_min_occurrences: int = 3  # require N high scores before marking strong

    # -- User field caps (tools.py update_preference, post_session.py) --
    max_interests: int = 8
    max_learning_goals: int = 8
    max_topics_to_avoid: int = 8
    max_additional_notes: int = 10
    max_interest_item_length: int = 100
    max_goal_item_length: int = 200
    max_topic_to_avoid_item_length: int = 100
    max_note_item_length: int = 200

    # -- Schedule limits --
    max_schedules_per_user: int = 10
    max_schedules_per_type: int = 3

    # -- Query / display limits --
    vocab_search_limit: int = 20
    exercise_history_max: int = 50
    due_vocab_max: int = 50
    due_vocab_default_limit: int = 30
    exercise_history_default_limit: int = 10
    progress_summary_default_days: int = 30
    progress_summary_max_days: int = 90
    recent_scores_display: int = 10

    # -- Exercise recording --
    max_exercise_words: int = 20
    max_exercise_type_length: int = 50
    max_topic_length: int = 100
    max_word_length: int = 200
    max_translation_length: int = 200

    # -- FSRS rating breakpoints (normalized 0-10 score → FSRS 1-4 rating) --
    fsrs_rating_fail_threshold: int = 3    # score <= this → rating 1 (Again)
    fsrs_rating_hard_threshold: int = 5    # score <= this → rating 2 (Hard)
    fsrs_rating_easy_threshold: int = 9    # score >= this → rating 4 (Easy)

    # -- FSRS scheduling steps (Telegram-friendly: hours/days, not minutes) --
    fsrs_learning_steps_minutes: tuple[int, ...] = (240, 1440)     # (4h, 1d)
    fsrs_relearning_steps_minutes: tuple[int, ...] = (480,)        # (8h,)
    fsrs_review_session_cap: int = 30                              # max cards per /words session

    # -- Weak/strong area recent topic limit --
    weak_strong_recent_limit: int = 10

    # -- Stale topic detection (session_manager._compute_stale_topics) --
    stale_topic_exercise_window: int = 100  # recent exercises to scan for stale topics
    stale_topic_days: int = 7  # days since last practice to consider a topic stale
    stale_topic_score: float = 7.0  # avg score <= this to flag as stale

    # -- Notification --
    notification_max_length: int = 2000
    notification_preview_length: int = 80

    # -- Streak --
    streak_grace_days: int = 2  # days before streak resets
    streak_decay_threshold: int = 30  # streaks >= this use decay instead of hard reset
    streak_decay_factor: float = 0.7  # keep 70% of streak on reset

    # -- Session history --
    session_history_cap: int = 15

    # -- Notification lookback --
    notification_lookback_hours: int = 24

    # -- Comeback adaptation --
    comeback_threshold_hours: float = 48
    comeback_short_max_hours: float = 72  # short comeback (<72h) vs full comeback (72h+)
    comeback_vocab_overload_threshold: int = 30  # due_count above this triggers simplified instructions
    comeback_min_due_for_review: int = 3  # minimum overdue cards to suggest review in comeback
    comeback_short_absence_days: int = 7  # boundary: short (3-7d) vs medium (1-3w)
    comeback_medium_absence_days: int = 21  # boundary: medium (1-3w) vs long (3w+)
    comeback_difficulty_full_override_days: int = 21  # gap_days >= this → full EASY override
    comeback_difficulty_adjust_days: int = 7  # gap_days >= this → partial difficulty adjustment
    comeback_struggling_avg: float = 5.0  # avg below this → extra scaffolding in comeback
    comeback_good_avg: float = 7.0  # avg above this → lighter warm-up in comeback
    comeback_stale_gap_days: int = 5  # gap_days >= this + good avg → still show warm-up
    comeback_advanced_vocab_threshold: int = 100  # vocab count for "advanced learner" detection

    # -- Hook hints --
    hook_rolling_avg_window: int = 3
    hook_struggling_threshold: float = 4.0
    hook_excelling_threshold: float = 8.5
    hook_single_struggling_threshold: int = 4
    hook_single_excellent_threshold: int = 9

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
    post_onboarding_max_hours: int = 336  # 14 days

    # -- Progress celebration trigger --
    progress_celebration_min_sessions: int = 3       # minimum sessions to be eligible
    progress_celebration_max_inactive_days: int = 7  # must have been active within this window
    progress_celebration_good_avg: float = 7.0       # avg score to celebrate "doing well"
    progress_celebration_min_vocab: int = 10          # minimum vocabulary for vocab celebration
    progress_celebration_min_streak: int = 3          # minimum streak days for streak celebration
    progress_celebration_chance_pct: int = 33         # ~1 in 3 chance per day (deterministic hash)

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
    summary_session_timeout_seconds: float = 30.0
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
    plan_max_focus_length: int = 200
    plan_max_topic_length: int = 100
    plan_max_vocab_theme_length: int = 100
    plan_max_vocab_target_per_week: int = 30
    plan_max_description_length: int = 500
    plan_max_adaptation_reason_length: int = 300
    plan_default_weekly_sessions: int = 4
    plan_auto_create_after_sessions: int = 3  # sessions > this → auto-create instead of propose

    # -- Web search --
    max_searches_per_session: int = 5  # shared counter for search + extract calls
    web_search_max_results: int = 5
    web_search_timeout_seconds: float = 10.0
    web_extract_max_content_chars: int = 5000  # truncate extracted page content

    # -- Prompt builder --
    prompt_sanitize_default_len: int = 200  # default max_len for _sanitize/_sanitize_list
    prompt_name_max_len: int = 50  # first_name truncation in prompt
    prompt_word_max_len: int = 50  # word display truncation in prompt context
    prompt_score_high_avg: float = 8.0  # avg above this → "performing well" hint
    prompt_score_low_avg: float = 4.0  # avg below this → "struggling" hint

    # -- Greeting thresholds (hours since last session) --
    greeting_continuation_hours: float = 1  # < this → continuation
    greeting_short_break_hours: float = 4  # < this → short_break
    greeting_normal_return_hours: float = 10  # < this → normal_return
    greeting_long_break_hours: float = 24  # < this → long_break (else day_plus_break or long_absence)

    # -- Time of day boundaries --
    time_of_day_night_end: int = 6  # hours: < this → "night"
    time_of_day_morning_end: int = 12  # hours: < this → "morning"
    time_of_day_afternoon_end: int = 17  # hours: < this → "afternoon", else "evening"

    # -- Knowledge gap hints (early sessions) --
    knowledge_gap_max_sessions: int = 5  # show hints for users with <= N sessions
    knowledge_gap_style_max_sessions: int = 2  # suggest style preference if <= N sessions

    # -- Level-specific vocab floor (prompt_builder vocabulary nudge) --
    level_vocab_floor: dict[str, int] = field(default_factory=lambda: {
        "A1": 40, "A2": 100, "B1": 200, "B2": 300, "C1": 500, "C2": 600,
    })

    # -- Plan pace assessment --
    plan_ahead_schedule_pct: float = 15.0  # pct ahead of expected to mark "AHEAD"

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
        max_cost_per_session_usd=0.60,
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
        max_cost_per_session_usd=2.25,
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

    # Whitelist
    whitelist_mode: bool = False  # WHITELIST_MODE env var; when True, only approved users can access

    # Logging
    log_level: str = "INFO"  # LOG_LEVEL env var; toggled at runtime via /settings

    # Web search (Tavily) — optional, tool disabled when empty
    tavily_api_key: str = ""  # TAVILY_API_KEY env var; free tier at tavily.com

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
