import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from adaptive_lang_study_bot.config import settings
from adaptive_lang_study_bot.enums import NotificationStatus, PipelineStatus, ScheduleStatus, SessionType
from adaptive_lang_study_bot.utils import compute_new_streak, compute_next_trigger, safe_zoneinfo, user_local_now
from adaptive_lang_study_bot.db.models import (
    AccessRequest,
    ExerciseResult,
    LearningPlan,
    Notification,
    Schedule,
    Session,
    User,
    Vocabulary,
    VocabularyReviewLog,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _today_start() -> datetime:
    """Start of today (UTC midnight) for date-range queries."""
    return _utcnow().replace(hour=0, minute=0, second=0, microsecond=0)


def _local_today_start(user_timezone: str) -> datetime:
    """Start of today in the user's local timezone, converted to UTC.

    Used for per-user date-range queries (session counts, cost limits)
    so that "today" aligns with the user's actual day boundary.
    """
    tz = safe_zoneinfo(user_timezone)
    local_now = datetime.now(tz)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc)


def _user_local_date(user: User) -> date:
    """Get today's date in the user's configured timezone.

    Delegates to the canonical user_local_now() to avoid duplicating
    timezone resolution logic.
    """
    return user_local_now(user).date()


def _escape_like(text: str) -> str:
    """Escape SQL LIKE wildcard characters in user input."""
    return text.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


# ---------------------------------------------------------------------------
# UserRepo
# ---------------------------------------------------------------------------

class UserRepo:

    @staticmethod
    async def get(session: AsyncSession, telegram_id: int) -> User | None:
        return await session.get(User, telegram_id)

    @staticmethod
    async def create(session: AsyncSession, **kwargs) -> User:
        user = User(**kwargs)
        session.add(user)
        await session.flush()
        return user

    @staticmethod
    async def delete(session: AsyncSession, telegram_id: int) -> bool:
        """Delete a user and all related data (CASCADE). Returns True if deleted."""
        result = await session.execute(
            delete(User).where(User.telegram_id == telegram_id),
        )
        return result.rowcount > 0

    @staticmethod
    async def update_fields(
        session: AsyncSession, telegram_id: int, **kwargs,
    ) -> None:
        kwargs["updated_at"] = _utcnow()
        await session.execute(
            update(User).where(User.telegram_id == telegram_id).values(**kwargs),
        )

    @staticmethod
    async def append_score(
        session: AsyncSession, telegram_id: int, score: int, *, max_len: int = 30,
    ) -> list[int]:
        """Atomically append a score to recent_scores (keep last `max_len`).

        Uses a single UPDATE with PostgreSQL array operations to avoid the
        read-modify-write race where concurrent calls lose scores.
        """
        # PostgreSQL array concatenation + slice in a single UPDATE.
        # SQLAlchemy doesn't support array subscript syntax natively,
        # so we use text() for the RETURNING expression.
        new_scores_expr = text(
            "(coalesce(recent_scores, ARRAY[]::smallint[]) || ARRAY[:score]::smallint[])"
            "[greatest(1, array_length("
            "coalesce(recent_scores, ARRAY[]::smallint[]) || ARRAY[:score]::smallint[], 1"
            ") - :max_len + 1):]"
        ).bindparams(score=score, max_len=max_len)

        result = await session.execute(
            update(User)
            .where(User.telegram_id == telegram_id)
            .values(
                recent_scores=new_scores_expr,
                updated_at=_utcnow(),
            )
            .returning(User.recent_scores),
        )
        row = result.one_or_none()
        if row is None:
            raise ValueError(f"User {telegram_id} not found")
        return list(row[0] or [])

    @staticmethod
    async def update_streak(session: AsyncSession, telegram_id: int) -> int:
        """Increment streak if new day, reset if gap > 1 day. Returns new streak.

        Uses the user's local date so streak tracking matches the trigger
        evaluation in ``check_streak_risk`` (which uses ``user_local_now``).

        The UPDATE includes an optimistic WHERE guard on streak_updated_at
        so that two concurrent post-session pipelines cannot both increment
        the streak for the same day.
        """
        user = await session.get(User, telegram_id)
        if user is None:
            raise ValueError(f"User {telegram_id} not found")
        today = _user_local_date(user)
        new_streak = compute_new_streak(user.streak_days, user.streak_updated_at, today)
        if user.streak_updated_at != today:
            result = await session.execute(
                update(User)
                .where(
                    User.telegram_id == telegram_id,
                    # Use IS DISTINCT FROM so NULL != today evaluates to True
                    # (plain != would produce NULL for NULL values in SQL).
                    User.streak_updated_at.is_distinct_from(today),
                )
                .values(
                    streak_days=new_streak,
                    streak_updated_at=today,
                    updated_at=_utcnow(),
                )
                .returning(User.streak_days),
            )
            row = result.one_or_none()
            if row is not None:
                # We won the update — use the returned value.
                user.streak_days = row[0]
                user.streak_updated_at = today
            else:
                # Another pipeline already updated the streak for today.
                # Re-read the actual DB value instead of returning stale cache.
                await session.refresh(user, attribute_names=["streak_days", "streak_updated_at"])
        return user.streak_days

    @staticmethod
    async def reset_notification_counter(
        session: AsyncSession, telegram_id: int, *, local_date: date,
    ) -> None:
        await session.execute(
            update(User)
            .where(User.telegram_id == telegram_id)
            .values(
                notifications_sent_today=0,
                notifications_count_reset_date=local_date,
                updated_at=_utcnow(),
            ),
        )

    @staticmethod
    async def increment_notification_count(
        session: AsyncSession, telegram_id: int,
    ) -> int:
        """Atomically increment notifications_sent_today using SQL expression.

        Avoids the read-modify-write race that occurs when incrementing
        in Python (two concurrent dispatches could both read the same
        value and lose an increment).
        """
        result = await session.execute(
            update(User)
            .where(User.telegram_id == telegram_id)
            .values(
                notifications_sent_today=User.notifications_sent_today + 1,
                updated_at=_utcnow(),
            )
            .returning(User.notifications_sent_today),
        )
        row = result.one_or_none()
        if row is None:
            raise ValueError(f"User {telegram_id} not found")
        return row[0]

    @staticmethod
    async def check_and_increment_notification(
        session: AsyncSession, telegram_id: int, max_per_day: int,
        *, local_date: date | None = None,
    ) -> bool:
        """Atomically reset counter if date changed, then check-and-increment.

        Returns True if the notification is allowed (count was under the
        limit and has been incremented), False if the limit was reached.

        When *local_date* is provided, the counter is reset if the stored
        reset date differs (handles day rollover atomically without a
        separate read-modify-write cycle).
        """
        # Reset counter if the date has changed.
        # Use IS DISTINCT FROM so NULL != local_date evaluates to True
        # (plain != produces NULL for NULL values in SQL, skipping the reset
        # entirely for new users whose notifications_count_reset_date is NULL).
        if local_date is not None:
            await session.execute(
                update(User)
                .where(
                    User.telegram_id == telegram_id,
                    User.notifications_count_reset_date.is_distinct_from(local_date),
                )
                .values(
                    notifications_sent_today=0,
                    notifications_count_reset_date=local_date,
                    updated_at=_utcnow(),
                )
            )

        # Atomic increment with limit check
        result = await session.execute(
            update(User)
            .where(
                User.telegram_id == telegram_id,
                User.notifications_sent_today < max_per_day,
            )
            .values(
                notifications_sent_today=User.notifications_sent_today + 1,
                updated_at=_utcnow(),
            )
            .returning(User.notifications_sent_today),
        )
        row = result.one_or_none()
        return row is not None

    @staticmethod
    async def get_active_users_for_proactive(
        session: AsyncSession,
        *,
        limit: int = 0,
        offset: int = 0,
    ) -> list[User]:
        """Get active users for proactive engine tick.

        When *limit* > 0, paginate results using a stable ordering by PK.
        """
        stmt = select(User).where(User.is_active.is_(True)).order_by(User.telegram_id)
        if limit > 0:
            stmt = stmt.limit(limit).offset(offset)
        result = await session.execute(stmt)
        return result.scalars().all()

    @staticmethod
    async def list_all(
        session: AsyncSession, *, active_only: bool = True,
    ) -> list[User]:
        stmt = select(User)
        if active_only:
            stmt = stmt.where(User.is_active.is_(True))
        stmt = stmt.order_by(User.created_at)
        result = await session.execute(stmt)
        return result.scalars().all()

    @staticmethod
    async def count(session: AsyncSession, *, active_only: bool = True) -> int:
        stmt = select(func.count()).select_from(User)
        if active_only:
            stmt = stmt.where(User.is_active.is_(True))
        result = await session.execute(stmt)
        return result.scalar_one()

    @staticmethod
    async def search(
        session: AsyncSession,
        query: str,
    ) -> list[User]:
        """Search users by name, username, or telegram ID (exact match)."""
        if query.isdigit():
            result = await session.execute(
                select(User).where(User.telegram_id == int(query)),
            )
        else:
            pattern = f"%{_escape_like(query)}%"
            result = await session.execute(
                select(User).where(
                    (User.first_name.ilike(pattern))
                    | (User.telegram_username.ilike(pattern))
                ).limit(50),
            )
        return result.scalars().all()

    @staticmethod
    async def get_tier_counts(session: AsyncSession) -> dict[str, int]:
        """Count active users by tier."""
        result = await session.execute(
            select(User.tier, func.count())
            .where(User.is_active.is_(True))
            .group_by(User.tier),
        )
        return dict(result.all())

    @staticmethod
    async def get_admins(session: AsyncSession) -> list[User]:
        """Get all users who are admins (DB flag only)."""
        result = await session.execute(
            select(User).where(User.is_admin.is_(True)),
        )
        return result.scalars().all()

    @staticmethod
    async def get_all_admin_ids(session: AsyncSession) -> list[int]:
        """Get all admin telegram IDs (DB flag + env var, deduplicated)."""
        result = await session.execute(
            select(User.telegram_id).where(User.is_admin.is_(True)),
        )
        db_ids = [row[0] for row in result.all()]
        all_ids = set(db_ids) | set(settings.admin_telegram_ids)
        return list(all_ids)

    @staticmethod
    async def clear_pending_celebrations(
        session: AsyncSession, telegram_id: int,
    ) -> None:
        """Atomically clear only the pending_celebrations key in milestones JSONB.

        Uses jsonb_set() so other milestone keys (vocabulary_count, days_streak)
        are preserved even if a concurrent post-session pipeline is updating them.
        """
        await session.execute(
            update(User)
            .where(User.telegram_id == telegram_id)
            .values(
                milestones=func.jsonb_set(
                    func.coalesce(User.milestones, text("'{}'::jsonb")),
                    text("'{pending_celebrations}'"),
                    text("'[]'::jsonb"),
                ),
                updated_at=_utcnow(),
            ),
        )

    _ALLOWED_MILESTONE_KEYS = frozenset({
        "pending_celebrations", "achieved", "streak", "vocab", "sessions",
        "onboarding_step", "vocabulary_count", "days_streak",
        "fired_streaks", "fired_vocab", "fired_sessions",
    })

    @staticmethod
    async def update_milestones(
        session: AsyncSession, telegram_id: int, milestones: dict,
    ) -> None:
        """Atomically merge milestone fields into the existing JSONB.

        Uses the ``||`` operator so keys not present in *milestones* are
        preserved, avoiding full-dict overwrites that lose concurrent changes.
        """
        invalid_keys = set(milestones.keys()) - UserRepo._ALLOWED_MILESTONE_KEYS
        if invalid_keys:
            raise ValueError(f"Invalid milestone keys: {invalid_keys}")

        await session.execute(
            update(User)
            .where(User.telegram_id == telegram_id)
            .values(
                milestones=func.coalesce(User.milestones, text("'{}'::jsonb")).op("||")(
                    func.cast(milestones, JSONB)
                ),
                updated_at=_utcnow(),
            ),
        )


# ---------------------------------------------------------------------------
# VocabularyRepo
# ---------------------------------------------------------------------------

class VocabularyRepo:

    @staticmethod
    async def add(session: AsyncSession, **kwargs) -> Vocabulary:
        vocab = Vocabulary(**kwargs)
        session.add(vocab)
        await session.flush()
        return vocab

    @staticmethod
    async def get(session: AsyncSession, vocab_id: int) -> Vocabulary | None:
        return await session.get(Vocabulary, vocab_id)

    @staticmethod
    async def get_by_word(
        session: AsyncSession, user_id: int, word: str,
    ) -> Vocabulary | None:
        result = await session.execute(
            select(Vocabulary).where(
                Vocabulary.user_id == user_id,
                Vocabulary.word == word,
            ),
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_word_ci(
        session: AsyncSession, user_id: int, word: str,
    ) -> Vocabulary | None:
        """Case-insensitive word lookup for dedup."""
        result = await session.execute(
            select(Vocabulary).where(
                Vocabulary.user_id == user_id,
                func.lower(Vocabulary.word) == word.lower(),
            ),
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_words_ci(
        session: AsyncSession, user_id: int, words: list[str],
    ) -> list[Vocabulary]:
        """Batch case-insensitive word lookup. Returns all matching vocab rows."""
        if not words:
            return []
        lower_words = [w.lower() for w in words]
        result = await session.execute(
            select(Vocabulary).where(
                Vocabulary.user_id == user_id,
                func.lower(Vocabulary.word).in_(lower_words),
            ),
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_due(
        session: AsyncSession,
        user_id: int,
        *,
        limit: int = 40,
        topic: str | None = None,
    ) -> list[Vocabulary]:
        """Fetch FSRS-due cards for a user, sorted by urgency (most overdue first)."""
        stmt = (
            select(Vocabulary)
            .where(
                Vocabulary.user_id == user_id,
                Vocabulary.fsrs_due <= _utcnow(),
            )
        )
        if topic:
            stmt = stmt.where(Vocabulary.topic == topic)
        stmt = stmt.order_by(Vocabulary.fsrs_due.asc()).limit(limit)
        result = await session.execute(stmt)
        return result.scalars().all()

    @staticmethod
    async def count_due(
        session: AsyncSession, user_id: int,
    ) -> int:
        result = await session.execute(
            select(func.count())
            .select_from(Vocabulary)
            .where(
                Vocabulary.user_id == user_id,
                Vocabulary.fsrs_due <= _utcnow(),
            ),
        )
        return result.scalar_one()

    @staticmethod
    async def count_due_batch(
        session: AsyncSession, user_ids: list[int],
    ) -> dict[int, int]:
        """Count due cards for multiple users in a single query.

        Returns a dict mapping user_id -> due_count (users with 0 due
        cards are included with value 0).
        """
        if not user_ids:
            return {}
        result = await session.execute(
            select(Vocabulary.user_id, func.count())
            .where(
                Vocabulary.user_id.in_(user_ids),
                Vocabulary.fsrs_due <= _utcnow(),
            )
            .group_by(Vocabulary.user_id),
        )
        counts = dict(result.all())
        return {uid: counts.get(uid, 0) for uid in user_ids}

    @staticmethod
    async def search(
        session: AsyncSession,
        user_id: int,
        query: str,
        *,
        limit: int = 20,
    ) -> list[Vocabulary]:
        """Search vocabulary by word or translation (case-insensitive)."""
        pattern = f"%{_escape_like(query)}%"
        result = await session.execute(
            select(Vocabulary)
            .where(
                Vocabulary.user_id == user_id,
                (Vocabulary.word.ilike(pattern) | Vocabulary.translation.ilike(pattern)),
            )
            .order_by(Vocabulary.word)
            .limit(limit),
        )
        return result.scalars().all()

    @staticmethod
    async def get_by_topic(
        session: AsyncSession,
        user_id: int,
        topic: str,
        *,
        limit: int = 50,
    ) -> list[Vocabulary]:
        result = await session.execute(
            select(Vocabulary)
            .where(Vocabulary.user_id == user_id, Vocabulary.topic == topic)
            .order_by(Vocabulary.word)
            .limit(limit),
        )
        return result.scalars().all()

    @staticmethod
    async def count_for_user(session: AsyncSession, user_id: int) -> int:
        result = await session.execute(
            select(func.count())
            .select_from(Vocabulary)
            .where(Vocabulary.user_id == user_id),
        )
        return result.scalar_one()

    @staticmethod
    async def count_added_since(
        session: AsyncSession, user_id: int, since: datetime,
    ) -> int:
        """Count vocabulary cards added for a user since a given timestamp."""
        result = await session.execute(
            select(func.count())
            .select_from(Vocabulary)
            .where(Vocabulary.user_id == user_id, Vocabulary.created_at >= since),
        )
        return result.scalar_one()

    @staticmethod
    async def get_state_counts(
        session: AsyncSession, user_id: int,
    ) -> dict[int, int]:
        """Count vocabulary cards by FSRS state (0=New, 1=Learning, 2=Review, 3=Relearning)."""
        result = await session.execute(
            select(Vocabulary.fsrs_state, func.count())
            .where(Vocabulary.user_id == user_id)
            .group_by(Vocabulary.fsrs_state),
        )
        return dict(result.all())

    @staticmethod
    async def update_fsrs(
        session: AsyncSession,
        vocab_id: int,
        *,
        fsrs_state: int,
        fsrs_stability: float | None,
        fsrs_difficulty: float | None,
        fsrs_due: datetime,
        fsrs_last_review: datetime,
        fsrs_data: dict,
        last_rating: int,
    ) -> None:
        await session.execute(
            update(Vocabulary)
            .where(Vocabulary.id == vocab_id)
            .values(
                fsrs_state=fsrs_state,
                fsrs_stability=fsrs_stability,
                fsrs_difficulty=fsrs_difficulty,
                fsrs_due=fsrs_due,
                fsrs_last_review=fsrs_last_review,
                fsrs_data=fsrs_data,
                last_rating=last_rating,
                review_count=Vocabulary.review_count + 1,
                updated_at=_utcnow(),
            ),
        )

    @staticmethod
    async def delete_for_user(session: AsyncSession, user_id: int) -> int:
        """Delete all vocabulary for a user. Returns count of deleted rows.

        Vocabulary_review_log rows cascade-delete via FK on vocabulary.id.
        """
        result = await session.execute(
            delete(Vocabulary).where(Vocabulary.user_id == user_id),
        )
        return result.rowcount

    @staticmethod
    async def get_global_summary(session: AsyncSession) -> dict[str, int]:
        """Aggregate vocabulary stats across all users: total words and total due."""
        result = await session.execute(
            select(
                func.count(),
                func.count(Vocabulary.id).filter(Vocabulary.fsrs_due <= _utcnow()),
            )
            .select_from(Vocabulary),
        )
        row = result.one()
        return {"total_words": row[0], "total_due": row[1]}

    @staticmethod
    async def get_per_user_summary(
        session: AsyncSession, *, limit: int = 20,
    ) -> list[tuple]:
        """Per-user vocab summary: (user_id, first_name, total, due).

        Sorted by total word count descending, limited to top N users.
        """
        result = await session.execute(
            select(
                Vocabulary.user_id,
                User.first_name,
                func.count(),
                func.count(Vocabulary.id).filter(Vocabulary.fsrs_due <= _utcnow()),
            )
            .join(User, Vocabulary.user_id == User.telegram_id)
            .group_by(Vocabulary.user_id, User.first_name)
            .order_by(func.count().desc())
            .limit(limit),
        )
        return list(result.all())


# ---------------------------------------------------------------------------
# SessionRepo
# ---------------------------------------------------------------------------

class SessionRepo:

    @staticmethod
    async def create(session: AsyncSession, **kwargs) -> Session:
        sess = Session(**kwargs)
        session.add(sess)
        await session.flush()
        return sess

    @staticmethod
    async def get(session: AsyncSession, session_id: uuid.UUID) -> Session | None:
        return await session.get(Session, session_id)

    @staticmethod
    async def update_end(
        session: AsyncSession,
        session_id: uuid.UUID,
        *,
        cost_usd: float = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
        num_turns: int = 0,
        tool_calls_count: int = 0,
        tool_calls_detail: dict | None = None,
        duration_ms: int | None = None,
    ) -> None:
        values: dict = {
            "ended_at": _utcnow(),
            "cost_usd": cost_usd,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_tokens": cache_creation_tokens,
            "cache_read_tokens": cache_read_tokens,
            "num_turns": num_turns,
            "tool_calls_count": tool_calls_count,
            "tool_calls_detail": tool_calls_detail,
            "duration_ms": duration_ms,
        }
        await session.execute(
            update(Session).where(Session.id == session_id).values(**values),
        )

    @staticmethod
    async def set_pipeline_status(
        session: AsyncSession,
        session_id: uuid.UUID,
        status: str,
        issues: dict | None = None,
    ) -> None:
        await session.execute(
            update(Session)
            .where(Session.id == session_id)
            .values(pipeline_status=status, pipeline_issues=issues),
        )

    @staticmethod
    async def set_ai_summary(
        session: AsyncSession,
        session_id: uuid.UUID,
        ai_summary: str,
    ) -> None:
        await session.execute(
            update(Session)
            .where(Session.id == session_id)
            .values(ai_summary=ai_summary),
        )

    @staticmethod
    async def get_recent_with_summaries(
        session: AsyncSession,
        user_id: int,
        *,
        limit: int = 3,
    ) -> list[Session]:
        """Fetch recent interactive/onboarding sessions that have an AI summary."""
        result = await session.execute(
            select(Session)
            .where(
                Session.user_id == user_id,
                Session.ai_summary.isnot(None),
                Session.session_type.in_(["interactive", "onboarding"]),
            )
            .order_by(Session.started_at.desc())
            .limit(limit),
        )
        return result.scalars().all()

    @staticmethod
    async def get_recent(
        session: AsyncSession,
        user_id: int,
        *,
        limit: int = 10,
    ) -> list[Session]:
        result = await session.execute(
            select(Session)
            .where(Session.user_id == user_id)
            .order_by(Session.started_at.desc())
            .limit(limit),
        )
        return result.scalars().all()

    @staticmethod
    async def count_today(
        session: AsyncSession, user_id: int, *, user_timezone: str = "UTC",
    ) -> int:
        today_start = _local_today_start(user_timezone)
        result = await session.execute(
            select(func.count())
            .select_from(Session)
            .where(
                Session.user_id == user_id,
                Session.started_at >= today_start,
                Session.session_type == SessionType.INTERACTIVE,
            ),
        )
        return result.scalar_one()

    @staticmethod
    async def get_total_cost_today(
        session: AsyncSession, user_id: int, *, user_timezone: str = "UTC",
    ) -> float:
        today_start = _local_today_start(user_timezone)
        result = await session.execute(
            select(func.coalesce(func.sum(Session.cost_usd), 0))
            .where(
                Session.user_id == user_id,
                Session.started_at >= today_start,
            ),
        )
        return float(result.scalar_one())

    @staticmethod
    async def list_recent_all(
        session: AsyncSession, *, limit: int = 50,
    ) -> list[Session]:
        """For admin panel — recent sessions across all users."""
        result = await session.execute(
            select(Session)
            .order_by(Session.started_at.desc())
            .limit(limit),
        )
        return result.scalars().all()

    @staticmethod
    async def get_daily_cost(session: AsyncSession, day: date) -> float:
        day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        next_day = day_start + timedelta(days=1)
        result = await session.execute(
            select(func.coalesce(func.sum(Session.cost_usd), 0))
            .where(Session.started_at >= day_start, Session.started_at < next_day),
        )
        return float(result.scalar_one())

    @staticmethod
    async def get_daily_costs_range(
        session: AsyncSession, *, days: int = 14,
    ) -> dict[date, float]:
        """Get daily costs for the last N days in a single query."""
        cutoff = datetime(
            *(_utcnow() - timedelta(days=days - 1)).timetuple()[:3],
            tzinfo=timezone.utc,
        )
        result = await session.execute(
            select(
                func.date(Session.started_at),
                func.coalesce(func.sum(Session.cost_usd), 0),
            )
            .where(Session.started_at >= cutoff)
            .group_by(func.date(Session.started_at))
        )
        return {row[0]: float(row[1]) for row in result.all()}

    @staticmethod
    async def get_cost_per_user(
        session: AsyncSession, *, days: int = 7,
    ) -> list[tuple[int, str, float, int]]:
        """Per-user cost summary over the last N days.

        Returns list of (user_id, first_name, total_cost, session_count).
        """
        cutoff = _utcnow() - timedelta(days=days)
        result = await session.execute(
            select(
                Session.user_id,
                User.first_name,
                func.coalesce(func.sum(Session.cost_usd), 0),
                func.count(Session.id),
            )
            .join(User, Session.user_id == User.telegram_id)
            .where(Session.started_at >= cutoff)
            .group_by(Session.user_id, User.first_name)
            .order_by(func.sum(Session.cost_usd).desc()),
        )
        return list(result.all())

    @staticmethod
    async def get_pipeline_failures(
        session: AsyncSession, *, limit: int = 30,
    ) -> list[Session]:
        """Get sessions where the post-session pipeline failed."""
        result = await session.execute(
            select(Session)
            .where(Session.pipeline_status.in_((PipelineStatus.FAILED, PipelineStatus.PENDING)))
            .order_by(Session.started_at.desc())
            .limit(limit),
        )
        return result.scalars().all()

    @staticmethod
    async def get_total_cost_range(
        session: AsyncSession, start: date, end: date,
    ) -> float:
        """Total cost over a date range (inclusive of both start and end days)."""
        start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        end_next = datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=1)
        result = await session.execute(
            select(func.coalesce(func.sum(Session.cost_usd), 0))
            .where(Session.started_at >= start_dt, Session.started_at < end_next),
        )
        return float(result.scalar_one())

    @staticmethod
    async def get_session_type_counts(
        session: AsyncSession, *, days: int = 7,
    ) -> dict[str, int]:
        """Count sessions by type over the last N days."""
        cutoff = _utcnow() - timedelta(days=days)
        result = await session.execute(
            select(Session.session_type, func.count())
            .where(Session.started_at >= cutoff)
            .group_by(Session.session_type),
        )
        return dict(result.all())

    @staticmethod
    async def get_daily_cost_average(
        session: AsyncSession, *, days: int = 7,
    ) -> float:
        """Average daily cost over the last N days (excluding today)."""
        today = _today_start()
        cutoff = today - timedelta(days=days)
        result = await session.execute(
            select(func.coalesce(func.sum(Session.cost_usd), 0))
            .where(Session.started_at.between(cutoff, today)),
        )
        total = float(result.scalar_one())
        return total / days if days > 0 else 0

    @staticmethod
    async def get_activity_stats(
        session: AsyncSession,
        user_id: int,
        *,
        days: int = 7,
    ) -> dict:
        """Session activity stats for a user over a time period."""
        cutoff = _utcnow() - timedelta(days=days)
        result = await session.execute(
            select(
                func.count(),
                func.avg(Session.duration_ms),
            ).where(
                Session.user_id == user_id,
                Session.started_at >= cutoff,
                Session.session_type == SessionType.INTERACTIVE,
            ),
        )
        row = result.one()
        return {
            "session_count": row[0],
            "avg_duration_ms": int(row[1]) if row[1] is not None else None,
        }

    @staticmethod
    async def count_today_all(session: AsyncSession) -> int:
        """Count all sessions started today (across all users)."""
        today_start = _today_start()
        result = await session.execute(
            select(func.count())
            .select_from(Session)
            .where(Session.started_at >= today_start),
        )
        return result.scalar_one()

    @staticmethod
    async def count_pipeline_failures_recent(
        session: AsyncSession, *, hours: int = 1,
    ) -> int:
        """Count pipeline failures in the last N hours."""
        cutoff = _utcnow() - timedelta(hours=hours)
        result = await session.execute(
            select(func.count())
            .select_from(Session)
            .where(
                Session.pipeline_status == PipelineStatus.FAILED,
                Session.started_at >= cutoff,
            ),
        )
        return result.scalar_one()

    @staticmethod
    async def count_since(
        session: AsyncSession, user_id: int, since: datetime,
    ) -> int:
        """Count interactive sessions for a user since a given timestamp."""
        result = await session.execute(
            select(func.count())
            .select_from(Session)
            .where(
                Session.user_id == user_id,
                Session.started_at >= since,
                Session.session_type == SessionType.INTERACTIVE,
            ),
        )
        return result.scalar_one()

    @staticmethod
    async def count_since_batch(
        session: AsyncSession,
        user_ids: list[int],
        since: datetime,
    ) -> dict[int, int]:
        """Count interactive sessions per user since *since* in one query."""
        if not user_ids:
            return {}
        result = await session.execute(
            select(Session.user_id, func.count())
            .where(
                Session.user_id.in_(user_ids),
                Session.started_at >= since,
                Session.session_type == SessionType.INTERACTIVE,
            )
            .group_by(Session.user_id),
        )
        return dict(result.all())

    @staticmethod
    async def get_daily_active_users(
        session: AsyncSession, *, days: int = 1,
    ) -> int:
        """Count distinct users with interactive sessions in the last N days."""
        cutoff = _utcnow() - timedelta(days=days)
        result = await session.execute(
            select(func.count(Session.user_id.distinct()))
            .where(
                Session.started_at >= cutoff,
                Session.session_type == SessionType.INTERACTIVE,
            ),
        )
        return result.scalar_one()

    @staticmethod
    async def get_avg_session_duration(
        session: AsyncSession, *, days: int = 7,
    ) -> float | None:
        """Average duration of interactive sessions in the last N days (ms)."""
        cutoff = _utcnow() - timedelta(days=days)
        result = await session.execute(
            select(func.avg(Session.duration_ms))
            .where(
                Session.started_at >= cutoff,
                Session.session_type == SessionType.INTERACTIVE,
                Session.duration_ms.isnot(None),
            ),
        )
        val = result.scalar_one_or_none()
        return float(val) if val is not None else None

    @staticmethod
    async def get_avg_sessions_per_user(
        session: AsyncSession, *, days: int = 7,
    ) -> float:
        """Average number of interactive sessions per active user in the last N days."""
        cutoff = _utcnow() - timedelta(days=days)
        result = await session.execute(
            select(
                func.count(),
                func.count(Session.user_id.distinct()),
            )
            .where(
                Session.started_at >= cutoff,
                Session.session_type == SessionType.INTERACTIVE,
            ),
        )
        row = result.one()
        total_sessions, unique_users = row[0], row[1]
        return total_sessions / unique_users if unique_users > 0 else 0.0


# ---------------------------------------------------------------------------
# ScheduleRepo
# ---------------------------------------------------------------------------

class ScheduleRepo:

    @staticmethod
    async def create(session: AsyncSession, **kwargs) -> Schedule:
        schedule = Schedule(**kwargs)
        session.add(schedule)
        await session.flush()
        return schedule

    @staticmethod
    async def get(session: AsyncSession, schedule_id: uuid.UUID) -> Schedule | None:
        return await session.get(Schedule, schedule_id)

    @staticmethod
    async def get_due(session: AsyncSession) -> list[Schedule]:
        """Get all active schedules whose next_trigger_at is in the past.

        Eagerly loads the user relationship to avoid N+1 queries in the tick loop.
        """
        result = await session.execute(
            select(Schedule)
            .options(selectinload(Schedule.user))
            .where(
                Schedule.status == ScheduleStatus.ACTIVE,
                Schedule.next_trigger_at <= _utcnow(),
            ),
        )
        return result.scalars().all()

    @staticmethod
    async def get_for_user(
        session: AsyncSession, user_id: int, *, active_only: bool = True,
    ) -> list[Schedule]:
        stmt = select(Schedule).where(Schedule.user_id == user_id)
        if active_only:
            stmt = stmt.where(Schedule.status == ScheduleStatus.ACTIVE)
        stmt = stmt.order_by(Schedule.created_at)
        result = await session.execute(stmt)
        return result.scalars().all()

    @staticmethod
    async def count_for_user(
        session: AsyncSession, user_id: int, *, active_only: bool = True,
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(Schedule)
            .where(Schedule.user_id == user_id)
        )
        if active_only:
            stmt = stmt.where(Schedule.status == ScheduleStatus.ACTIVE)
        result = await session.execute(stmt)
        return result.scalar_one()

    @staticmethod
    async def recalculate_triggers_for_user(
        session: AsyncSession, user_id: int, new_tz: str,
    ) -> int:
        """Recalculate next_trigger_at for all active schedules after timezone change.

        Returns the number of schedules updated.
        """
        user_tz = safe_zoneinfo(new_tz)
        schedules = await ScheduleRepo.get_for_user(session, user_id, active_only=True)
        updated = 0
        for sched in schedules:
            try:
                next_utc = compute_next_trigger(sched.rrule, user_tz)
                if next_utc is not None:
                    await ScheduleRepo.update_fields(
                        session, sched.id, next_trigger_at=next_utc,
                    )
                    updated += 1
            except (ValueError, TypeError):
                continue  # skip invalid RRULE strings
        return updated

    @staticmethod
    async def get_statuses_batch(
        session: AsyncSession, schedule_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, tuple[str, datetime | None]]:
        """Fetch current status and pause_until for multiple schedules in one query."""
        if not schedule_ids:
            return {}
        stmt = select(Schedule.id, Schedule.status, Schedule.pause_until).where(
            Schedule.id.in_(schedule_ids)
        )
        rows = (await session.execute(stmt)).all()
        return {row[0]: (row[1], row[2]) for row in rows}

    @staticmethod
    async def update_after_trigger(
        session: AsyncSession,
        schedule_id: uuid.UUID,
        *,
        next_trigger_at: datetime,
        success: bool = True,
    ) -> None:
        values: dict = {
            "last_triggered_at": _utcnow(),
            "trigger_count": Schedule.trigger_count + 1,
            "next_trigger_at": next_trigger_at,
            "updated_at": _utcnow(),
        }
        if success:
            values["consecutive_failures"] = 0
        else:
            values["consecutive_failures"] = Schedule.consecutive_failures + 1
        await session.execute(
            update(Schedule).where(Schedule.id == schedule_id).values(**values),
        )

    @staticmethod
    async def update_fields(
        session: AsyncSession, schedule_id: uuid.UUID, **kwargs,
    ) -> None:
        kwargs["updated_at"] = _utcnow()
        await session.execute(
            update(Schedule).where(Schedule.id == schedule_id).values(**kwargs),
        )

    @staticmethod
    async def delete(session: AsyncSession, schedule_id: uuid.UUID) -> None:
        await session.execute(
            delete(Schedule).where(Schedule.id == schedule_id),
        )

    @staticmethod
    async def delete_for_user(
        session: AsyncSession, user_id: int, schedule_type: str,
    ) -> int:
        result = await session.execute(
            delete(Schedule).where(
                Schedule.user_id == user_id,
                Schedule.schedule_type == schedule_type,
            ),
        )
        return result.rowcount


# ---------------------------------------------------------------------------
# ExerciseResultRepo
# ---------------------------------------------------------------------------

class ExerciseResultRepo:

    @staticmethod
    async def create(session: AsyncSession, **kwargs) -> ExerciseResult:
        result = ExerciseResult(**kwargs)
        session.add(result)
        await session.flush()
        return result

    @staticmethod
    async def get_recent(
        session: AsyncSession,
        user_id: int,
        *,
        limit: int = 20,
        topic: str | None = None,
    ) -> list[ExerciseResult]:
        stmt = (
            select(ExerciseResult)
            .where(ExerciseResult.user_id == user_id)
        )
        if topic:
            stmt = stmt.where(ExerciseResult.topic == topic)
        stmt = stmt.order_by(ExerciseResult.created_at.desc()).limit(limit)
        result = await session.execute(stmt)
        return result.scalars().all()

    @staticmethod
    async def get_by_session(
        session: AsyncSession,
        session_id: uuid.UUID,
    ) -> list[ExerciseResult]:
        """Get all exercise results from a specific session, ordered by time."""
        result = await session.execute(
            select(ExerciseResult)
            .where(ExerciseResult.session_id == session_id)
            .order_by(ExerciseResult.created_at.asc()),
        )
        return result.scalars().all()

    @staticmethod
    async def get_topic_average(
        session: AsyncSession,
        user_id: int,
        topic: str,
        *,
        last_n: int = 5,
    ) -> float | None:
        """Average normalized (0-10) score for a topic over the last N exercises."""
        normalized = ExerciseResult.score * 10.0 / ExerciseResult.max_score
        subq = (
            select(normalized.label("norm_score"))
            .where(
                ExerciseResult.user_id == user_id,
                ExerciseResult.topic == topic,
            )
            .order_by(ExerciseResult.created_at.desc())
            .limit(last_n)
            .subquery()
        )
        result = await session.execute(
            select(func.avg(subq.c.norm_score)),
        )
        val = result.scalar_one_or_none()
        return round(float(val), 1) if val is not None else None

    @staticmethod
    async def get_score_summary(
        session: AsyncSession,
        user_id: int,
        *,
        days: int | None = 30,
    ) -> dict:
        """Aggregate score statistics for a time period (None = all time)."""
        normalized = ExerciseResult.score * 10.0 / ExerciseResult.max_score
        conditions = [ExerciseResult.user_id == user_id]
        if days is not None:
            conditions.append(ExerciseResult.created_at >= _utcnow() - timedelta(days=days))
        result = await session.execute(
            select(
                func.count(),
                func.avg(normalized),
                func.min(normalized),
                func.max(normalized),
            ).where(*conditions),
        )
        row = result.one()
        return {
            "count": row[0],
            "avg": round(float(row[1]), 1) if row[1] is not None else None,
            "min": row[2],
            "max": row[3],
        }

    @staticmethod
    async def get_topic_stats(
        session: AsyncSession,
        user_id: int,
        *,
        days: int = 30,
        limit: int = 10,
    ) -> list[dict]:
        """Per-topic performance stats for a time period."""
        normalized = ExerciseResult.score * 10.0 / ExerciseResult.max_score
        cutoff = _utcnow() - timedelta(days=days)
        result = await session.execute(
            select(
                ExerciseResult.topic,
                func.count(),
                func.avg(normalized),
                func.max(ExerciseResult.created_at),
            )
            .where(
                ExerciseResult.user_id == user_id,
                ExerciseResult.created_at >= cutoff,
            )
            .group_by(ExerciseResult.topic)
            .order_by(func.count().desc())
            .limit(limit),
        )
        return [
            {
                "topic": row[0],
                "exercise_count": row[1],
                "avg_score": round(float(row[2]), 1) if row[2] else None,
                "last_practiced": row[3].strftime("%Y-%m-%d") if row[3] else None,
            }
            for row in result.all()
        ]

    @staticmethod
    async def count_for_user(session: AsyncSession, user_id: int) -> int:
        result = await session.execute(
            select(func.count())
            .select_from(ExerciseResult)
            .where(ExerciseResult.user_id == user_id),
        )
        return result.scalar_one()

    @staticmethod
    async def get_stats_for_topics(
        session: AsyncSession,
        user_id: int,
        topics: list[str],
        since: date,
    ) -> dict[str, dict]:
        """Per-topic stats for specific topics since a given date.

        Uses case-insensitive matching so plan topic names align with
        exercise topic names even when casing differs.
        """
        if not topics:
            return {}
        since_dt = datetime(since.year, since.month, since.day, tzinfo=timezone.utc)
        lower_topics = [t.lower() for t in topics]
        normalized = ExerciseResult.score * 10.0 / ExerciseResult.max_score
        result = await session.execute(
            select(
                ExerciseResult.topic,
                func.count(),
                func.avg(normalized),
                func.max(ExerciseResult.created_at),
            )
            .where(
                ExerciseResult.user_id == user_id,
                ExerciseResult.created_at >= since_dt,
                func.lower(ExerciseResult.topic).in_(lower_topics),
            )
            .group_by(ExerciseResult.topic),
        )
        # Build a case-insensitive lookup: map lowercase → original plan topic
        lower_to_plan: dict[str, str] = {}
        for t in topics:
            low = t.lower()
            if low not in lower_to_plan:
                lower_to_plan[low] = t
        stats: dict[str, dict] = {}
        for row in result.all():
            plan_topic = lower_to_plan.get(row[0].lower(), row[0])
            stats[plan_topic] = {
                "count": row[1],
                "avg_score": round(float(row[2]), 1) if row[2] is not None else None,
                "last_practiced": row[3].strftime("%Y-%m-%d") if row[3] else None,
            }
        return stats

    @staticmethod
    async def delete_for_user(session: AsyncSession, user_id: int) -> int:
        """Delete all exercise results for a user. Returns count of deleted rows."""
        result = await session.execute(
            delete(ExerciseResult).where(ExerciseResult.user_id == user_id),
        )
        return result.rowcount

    @staticmethod
    async def count_all(
        session: AsyncSession, *, days: int = 30,
    ) -> int:
        """Total exercise count across all users in the last N days."""
        cutoff = _utcnow() - timedelta(days=days)
        result = await session.execute(
            select(func.count())
            .select_from(ExerciseResult)
            .where(ExerciseResult.created_at >= cutoff),
        )
        return result.scalar_one()

    @staticmethod
    async def get_global_topic_stats(
        session: AsyncSession, *, days: int = 30, limit: int = 20,
    ) -> list[dict]:
        """Per-topic performance stats across ALL users."""
        normalized = ExerciseResult.score * 10.0 / ExerciseResult.max_score
        cutoff = _utcnow() - timedelta(days=days)
        result = await session.execute(
            select(
                ExerciseResult.topic,
                func.count(),
                func.avg(normalized),
                func.count(ExerciseResult.user_id.distinct()),
                func.max(ExerciseResult.created_at),
            )
            .where(ExerciseResult.created_at >= cutoff)
            .group_by(ExerciseResult.topic)
            .order_by(func.count().desc())
            .limit(limit),
        )
        return [
            {
                "topic": row[0],
                "exercise_count": row[1],
                "avg_score": round(float(row[2]), 1) if row[2] else None,
                "unique_users": row[3],
                "last_practiced": row[4].strftime("%Y-%m-%d") if row[4] else None,
            }
            for row in result.all()
        ]

    @staticmethod
    async def get_global_score_distribution(
        session: AsyncSession, *, days: int = 30,
    ) -> list[tuple[int, int]]:
        """Count exercises per score value (0-10) across all users."""
        cutoff = _utcnow() - timedelta(days=days)
        result = await session.execute(
            select(ExerciseResult.score, func.count())
            .where(ExerciseResult.created_at >= cutoff)
            .group_by(ExerciseResult.score)
            .order_by(ExerciseResult.score),
        )
        return list(result.all())


# ---------------------------------------------------------------------------
# NotificationRepo
# ---------------------------------------------------------------------------

class NotificationRepo:

    @staticmethod
    async def create(session: AsyncSession, **kwargs) -> Notification:
        notif = Notification(**kwargs)
        session.add(notif)
        await session.flush()
        return notif

    @staticmethod
    async def get_recent(
        session: AsyncSession,
        user_id: int,
        *,
        limit: int = 20,
    ) -> list[Notification]:
        result = await session.execute(
            select(Notification)
            .where(Notification.user_id == user_id)
            .order_by(Notification.created_at.desc())
            .limit(limit),
        )
        return result.scalars().all()

    @staticmethod
    async def count_sent_today(session: AsyncSession, user_id: int) -> int:
        today_start = _today_start()
        result = await session.execute(
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.status == NotificationStatus.SENT,
                Notification.created_at >= today_start,
            ),
        )
        return result.scalar_one()

    @staticmethod
    async def list_recent_all(
        session: AsyncSession, *, limit: int = 50,
    ) -> list[Notification]:
        """Recent notifications across all users (admin)."""
        result = await session.execute(
            select(Notification)
            .order_by(Notification.created_at.desc())
            .limit(limit),
        )
        return result.scalars().all()

    @staticmethod
    async def get_status_counts(
        session: AsyncSession, *, days: int = 7,
    ) -> dict[str, int]:
        """Count notifications by status over the last N days."""
        cutoff = _utcnow() - timedelta(days=days)
        result = await session.execute(
            select(Notification.status, func.count())
            .where(Notification.created_at >= cutoff)
            .group_by(Notification.status),
        )
        return dict(result.all())

    @staticmethod
    async def get_failure_rate_recent(
        session: AsyncSession, *, hours: int = 1,
    ) -> tuple[int, int]:
        """Return (failed_count, total_count) for notifications in the last N hours."""
        cutoff = _utcnow() - timedelta(hours=hours)
        result = await session.execute(
            select(Notification.status, func.count())
            .where(Notification.created_at >= cutoff)
            .group_by(Notification.status),
        )
        counts = dict(result.all())
        total = sum(counts.values())
        failed = counts.get(NotificationStatus.FAILED, 0)
        return failed, total


# ---------------------------------------------------------------------------
# VocabularyReviewLogRepo
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# LearningPlanRepo
# ---------------------------------------------------------------------------


class LearningPlanRepo:
    """One row per user max.  Existence of a row means the plan is active."""

    @staticmethod
    async def get_active(session: AsyncSession, user_id: int) -> LearningPlan | None:
        """Get the user's active learning plan (at most one due to UNIQUE on user_id)."""
        result = await session.execute(
            select(LearningPlan).where(LearningPlan.user_id == user_id),
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def create(session: AsyncSession, **kwargs) -> LearningPlan:
        """Create a new plan.  Deletes any existing plan for this user first."""
        user_id = kwargs.get("user_id")
        if user_id is not None:
            await session.execute(
                delete(LearningPlan).where(LearningPlan.user_id == user_id),
            )
        plan = LearningPlan(**kwargs)
        session.add(plan)
        await session.flush()
        return plan

    @staticmethod
    async def update_fields(
        session: AsyncSession,
        plan_id: uuid.UUID,
        **kwargs,
    ) -> None:
        kwargs["updated_at"] = _utcnow()
        await session.execute(
            update(LearningPlan).where(LearningPlan.id == plan_id).values(**kwargs),
        )

    @staticmethod
    async def delete(session: AsyncSession, user_id: int) -> None:
        """Delete the user's plan (plan completed or abandoned)."""
        await session.execute(
            delete(LearningPlan).where(LearningPlan.user_id == user_id),
        )

    @staticmethod
    async def list_all_with_user(session: AsyncSession) -> list[LearningPlan]:
        """Get all active learning plans with user relationship eagerly loaded."""
        result = await session.execute(
            select(LearningPlan).options(selectinload(LearningPlan.user)),
        )
        return result.scalars().all()

    @staticmethod
    async def count_all(session: AsyncSession) -> int:
        """Count all active learning plans."""
        result = await session.execute(
            select(func.count()).select_from(LearningPlan),
        )
        return result.scalar_one()


# ---------------------------------------------------------------------------
# VocabularyReviewLogRepo
# ---------------------------------------------------------------------------


class VocabularyReviewLogRepo:

    @staticmethod
    async def create(session: AsyncSession, **kwargs) -> VocabularyReviewLog:
        log = VocabularyReviewLog(**kwargs)
        session.add(log)
        await session.flush()
        return log

    @staticmethod
    async def get_for_vocab(
        session: AsyncSession,
        vocabulary_id: int,
        *,
        limit: int = 20,
    ) -> list[VocabularyReviewLog]:
        result = await session.execute(
            select(VocabularyReviewLog)
            .where(VocabularyReviewLog.vocabulary_id == vocabulary_id)
            .order_by(VocabularyReviewLog.created_at.desc())
            .limit(limit),
        )
        return result.scalars().all()


# ---------------------------------------------------------------------------
# AccessRequestRepo
# ---------------------------------------------------------------------------

class AccessRequestRepo:

    @staticmethod
    async def create(session: AsyncSession, **kwargs) -> AccessRequest:
        request = AccessRequest(**kwargs)
        session.add(request)
        await session.flush()
        return request

    @staticmethod
    async def get_pending(session: AsyncSession) -> list[AccessRequest]:
        result = await session.execute(
            select(AccessRequest)
            .where(AccessRequest.status == "pending")
            .order_by(AccessRequest.created_at),
        )
        return result.scalars().all()

    @staticmethod
    async def get_all(session: AsyncSession, *, limit: int = 100) -> list[AccessRequest]:
        result = await session.execute(
            select(AccessRequest)
            .order_by(AccessRequest.created_at.desc())
            .limit(limit),
        )
        return result.scalars().all()

    @staticmethod
    async def get_by_telegram_id(
        session: AsyncSession, telegram_id: int, *, status: str | None = None,
    ) -> list[AccessRequest]:
        stmt = select(AccessRequest).where(AccessRequest.telegram_id == telegram_id)
        if status:
            stmt = stmt.where(AccessRequest.status == status)
        stmt = stmt.order_by(AccessRequest.created_at.desc())
        result = await session.execute(stmt)
        return result.scalars().all()

    @staticmethod
    async def count_pending(session: AsyncSession) -> int:
        result = await session.execute(
            select(func.count())
            .select_from(AccessRequest)
            .where(AccessRequest.status == "pending"),
        )
        return result.scalar_one()

    @staticmethod
    async def update_status(
        session: AsyncSession,
        request_id: int,
        status: str,
        reviewed_by: int,
    ) -> None:
        await session.execute(
            update(AccessRequest)
            .where(AccessRequest.id == request_id)
            .values(
                status=status,
                reviewed_at=_utcnow(),
                reviewed_by=reviewed_by,
            ),
        )
