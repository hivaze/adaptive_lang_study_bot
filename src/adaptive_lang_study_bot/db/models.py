import uuid
from datetime import date, datetime, time, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    Time,
    column,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    # Identity
    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_username: Mapped[str | None] = mapped_column(String(255))
    first_name: Mapped[str] = mapped_column(String(255))

    # Learning configuration
    native_language: Mapped[str] = mapped_column(String(10))
    target_language: Mapped[str] = mapped_column(String(10))
    level: Mapped[str] = mapped_column(
        String(2), default="A1",
    )

    # Personalization (mutable by user)
    interests: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    learning_goals: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    preferred_difficulty: Mapped[str] = mapped_column(
        String(10), default="normal",
    )
    session_style: Mapped[str] = mapped_column(
        String(12), default="structured",
    )
    topics_to_avoid: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)

    # Performance tracking (system-managed)
    weak_areas: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    strong_areas: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    recent_scores: Mapped[list[int]] = mapped_column(ARRAY(SmallInteger), default=list)
    vocabulary_count: Mapped[int] = mapped_column(Integer, default=0)
    streak_days: Mapped[int] = mapped_column(Integer, default=0)
    streak_updated_at: Mapped[date | None] = mapped_column(Date)

    # Session continuity
    last_activity: Mapped[dict | None] = mapped_column(JSONB)
    session_history: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    last_session_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sessions_completed: Mapped[int] = mapped_column(Integer, default=0)

    # Milestones
    milestones: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Notification preferences (inlined for performance)
    timezone: Mapped[str] = mapped_column(String(50), default="UTC")
    quiet_hours_start: Mapped[time | None] = mapped_column(Time)
    quiet_hours_end: Mapped[time | None] = mapped_column(Time)
    notifications_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    max_notifications_per_day: Mapped[int] = mapped_column(SmallInteger, default=3)
    notifications_sent_today: Mapped[int] = mapped_column(SmallInteger, default=0)
    notifications_count_reset_date: Mapped[date | None] = mapped_column(Date)
    notification_preferences: Mapped[dict] = mapped_column(
        JSONB,
        default=lambda: {
            "streak_reminders": True,
            "vocab_reviews": True,
            "progress_reports": True,
            "re_engagement": True,
            "learning_nudges": True,
        },
    )

    # Proactive-to-interactive transition
    last_notification_text: Mapped[str | None] = mapped_column(Text)
    last_notification_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Tier
    tier: Mapped[str] = mapped_column(
        String(10), default="free",
    )

    # Onboarding
    onboarding_completed: Mapped[bool] = mapped_column(Boolean, default=False)

    # Metadata
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
    )

    # Relationships — lazy="raise" prevents accidental lazy loads in async context
    # (would raise MissingGreenlet with asyncpg). Use explicit repository queries instead.
    vocabulary: Mapped[list["Vocabulary"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="raise",
    )
    sessions: Mapped[list["Session"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="raise",
    )
    schedules: Mapped[list["Schedule"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="raise",
    )
    exercise_results: Mapped[list["ExerciseResult"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="raise",
    )
    notifications: Mapped[list["Notification"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="raise",
    )

    __table_args__ = (
        CheckConstraint("level IN ('A1','A2','B1','B2','C1','C2')", name="ck_users_level"),
        CheckConstraint(
            "preferred_difficulty IN ('easy','normal','hard')",
            name="ck_users_difficulty",
        ),
        CheckConstraint(
            "session_style IN ('casual','structured','intensive')",
            name="ck_users_style",
        ),
        CheckConstraint("tier IN ('free','premium')", name="ck_users_tier"),
        Index("idx_users_active_last_session", "last_session_at", postgresql_where="is_active = TRUE"),
        Index(
            "idx_users_notif_reset",
            "notifications_count_reset_date",
            postgresql_where="is_active = TRUE",
        ),
        Index("idx_users_is_admin", "telegram_id", postgresql_where="is_admin = TRUE"),
        Index("idx_users_created_at", "created_at"),
        # Covers the proactive tick pagination query:
        # SELECT ... WHERE is_active = TRUE ORDER BY telegram_id
        Index("idx_users_active_paging", "telegram_id", postgresql_where="is_active = TRUE"),
    )


class Vocabulary(Base):
    __tablename__ = "vocabulary"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"),
    )

    # Word data
    word: Mapped[str] = mapped_column(String(200))
    translation: Mapped[str | None] = mapped_column(String(200))
    context_sentence: Mapped[str | None] = mapped_column(Text)
    topic: Mapped[str | None] = mapped_column(String(100))

    # FSRS card state
    fsrs_state: Mapped[int] = mapped_column(SmallInteger, default=0)
    fsrs_stability: Mapped[float | None] = mapped_column(Double)
    fsrs_difficulty: Mapped[float | None] = mapped_column(Double)
    fsrs_due: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )
    fsrs_last_review: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fsrs_data: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Review stats
    review_count: Mapped[int] = mapped_column(Integer, default=0)
    last_rating: Mapped[int | None] = mapped_column(SmallInteger)

    # Metadata
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
    )

    # Relationships
    user: Mapped["User"] = relationship(back_populates="vocabulary", lazy="raise")
    review_logs: Mapped[list["VocabularyReviewLog"]] = relationship(
        back_populates="vocabulary", cascade="all, delete-orphan", lazy="raise",
    )

    __table_args__ = (
        Index("uq_user_word_ci", "user_id", func.lower(column("word")), unique=True),
        Index("idx_vocabulary_due", "user_id", "fsrs_due"),
        Index("idx_vocabulary_user_topic", "user_id", "topic"),
    )


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"),
    )

    session_type: Mapped[str] = mapped_column(String(20))

    # Cost tracking
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 6, asdecimal=False), default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)

    # Session metrics
    num_turns: Mapped[int] = mapped_column(SmallInteger, default=0)
    tool_calls_count: Mapped[int] = mapped_column(SmallInteger, default=0)
    tool_calls_detail: Mapped[dict | None] = mapped_column(JSONB)
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    # Post-session pipeline
    pipeline_status: Mapped[str] = mapped_column(String(20), default="pending")
    pipeline_issues: Mapped[dict | None] = mapped_column(JSONB)

    # Metadata
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Relationships
    user: Mapped["User"] = relationship(back_populates="sessions", lazy="raise")

    __table_args__ = (
        CheckConstraint(
            "session_type IN ('interactive','onboarding','proactive_review',"
            "'proactive_quiz','proactive_summary','proactive_nudge','assessment')",
            name="ck_sessions_type",
        ),
        CheckConstraint(
            "pipeline_status IN ('pending','running','completed','failed')",
            name="ck_sessions_pipeline",
        ),
        Index("idx_sessions_user_recent", "user_id", "started_at"),
        Index(
            "idx_sessions_pipeline",
            "pipeline_status",
            "started_at",
            postgresql_where="pipeline_status IN ('pending', 'failed')",
        ),
    )


class Schedule(Base):
    __tablename__ = "schedules"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"),
    )

    # Schedule definition
    schedule_type: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(10), default="active")

    # Recurrence (RFC 5545 RRULE)
    rrule: Mapped[str] = mapped_column(Text)

    pause_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Execution tracking
    next_trigger_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trigger_count: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_failures: Mapped[int] = mapped_column(SmallInteger, default=0)

    # Notification tier
    notification_tier: Mapped[str] = mapped_column(String(10), default="template")

    # Metadata
    description: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(20), default="system")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
    )

    # Relationships
    user: Mapped["User"] = relationship(back_populates="schedules", lazy="raise")

    __table_args__ = (
        CheckConstraint(
            "schedule_type IN ('daily_review','quiz','progress_report','practice_reminder','custom')",
            name="ck_schedules_type",
        ),
        CheckConstraint(
            "status IN ('active','paused','expired')",
            name="ck_schedules_status",
        ),
        CheckConstraint(
            "notification_tier IN ('template','llm','hybrid')",
            name="ck_schedules_tier",
        ),
        CheckConstraint(
            "created_by IN ('system','user','onboarding')",
            name="ck_schedules_created_by",
        ),
        Index(
            "idx_schedules_due",
            "next_trigger_at",
            postgresql_where="status = 'active'",
        ),
        Index("idx_schedules_user", "user_id", "status"),
    )


class ExerciseResult(Base):
    __tablename__ = "exercise_results"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"),
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="SET NULL"),
    )

    # Exercise data
    exercise_type: Mapped[str] = mapped_column(String(50))
    topic: Mapped[str] = mapped_column(String(100))
    score: Mapped[int] = mapped_column(SmallInteger)
    max_score: Mapped[int] = mapped_column(SmallInteger, default=10)

    # Detail
    words_involved: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    agent_notes: Mapped[str | None] = mapped_column(Text)

    # Metadata
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )

    # Relationships
    user: Mapped["User"] = relationship(back_populates="exercise_results", lazy="raise")

    __table_args__ = (
        CheckConstraint("score BETWEEN 0 AND 10", name="ck_exercise_score"),
        Index("idx_exercise_results_user_time", "user_id", "created_at"),
        Index("idx_exercise_results_user_topic", "user_id", "topic", "score"),
        Index("idx_exercise_results_session", "session_id"),
    )


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"),
    )
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("schedules.id", ondelete="SET NULL"),
    )

    # Notification data
    notification_type: Mapped[str] = mapped_column(String(30))
    tier: Mapped[str] = mapped_column(String(10))
    trigger_source: Mapped[str] = mapped_column(String(20))

    # Content
    message_text: Mapped[str] = mapped_column(Text)

    # Delivery
    status: Mapped[str] = mapped_column(String(15), default="sent")
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)

    # Cost (LLM tier only)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="SET NULL"),
    )
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 6, asdecimal=False), default=0)

    # Metadata
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )

    # Relationships
    user: Mapped["User"] = relationship(back_populates="notifications", lazy="raise")

    __table_args__ = (
        CheckConstraint(
            "status IN ('sent','skipped_quiet','skipped_paused','skipped_preference','skipped_limit','skipped_dedup','failed')",
            name="ck_notifications_status",
        ),
        Index("idx_notifications_user_time", "user_id", "created_at"),
        Index("idx_notifications_schedule", "schedule_id"),
        Index("idx_notifications_session", "session_id"),
    )


class VocabularyReviewLog(Base):
    __tablename__ = "vocabulary_review_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"),
    )
    vocabulary_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("vocabulary.id", ondelete="CASCADE"),
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="SET NULL"),
    )

    rating: Mapped[int] = mapped_column(SmallInteger)

    # Metadata
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )

    # Relationships
    vocabulary: Mapped["Vocabulary"] = relationship(back_populates="review_logs", lazy="raise")

    __table_args__ = (
        CheckConstraint("rating BETWEEN 1 AND 4", name="ck_review_rating"),
        Index("idx_review_log_user", "user_id", "created_at"),
        Index("idx_review_log_vocab", "vocabulary_id", "created_at"),
    )
