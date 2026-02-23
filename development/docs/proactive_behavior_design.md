# Proactive Behavior: Technical Design

## Problem

The bot must proactively reach out to users — not just respond to messages. Proactive behavior includes:
1. **System-driven**: Spaced repetition reviews, streak-at-risk alerts, progress summaries
2. **User-driven**: Custom reminders, recurring task requests, scheduled quizzes
3. **Event-driven**: Milestone celebrations, inactivity re-engagement, difficulty adjustments

All proactive behavior must be:
- Cost-aware (each LLM session costs ~$0.05, templates are free)
- Smart (context-aware timing, not dumb cron spam)
- User-controllable (pause, customize, opt-out)
- Timezone-aware
- Persistent across bot restarts

---

## Architecture Overview

```
                     ┌─────────────────────────────────────┐
                     │        Proactive Engine              │
                     │                                      │
  ┌──────────┐      │  ┌────────────────────────────────┐  │
  │ Scheduler│──────┤  │ tick_scheduler() — every 1 min  │  │
  │ (APSched)│      │  │  1. Query due schedules (DB)    │  │
  └──────────┘      │  │  2. Evaluate event triggers     │  │
                     │  │  3. Check quiet hours + limits  │  │
                     │  │  4. Dispatch notifications      │  │
                     │  └──────────┬─────────────────────┘  │
                     │             │                         │
                     │  ┌──────────▼─────────────────────┐  │
                     │  │     Notification Dispatcher      │  │
                     │  │                                  │  │
                     │  │  Template-based ($0.00):          │  │
                     │  │    streak saver, milestones,      │  │
                     │  │    incomplete exercise nudge      │  │
                     │  │                                  │  │
                     │  │  LLM-powered ($0.05):             │  │
                     │  │    vocabulary review session,      │  │
                     │  │    weekly progress summary,        │  │
                     │  │    re-engagement after inactivity  │  │
                     │  └──────────┬─────────────────────┘  │
                     │             │                         │
                     └─────────────┼─────────────────────────┘
                                   │
                     ┌─────────────▼─────────────────────┐
                     │    Telegram (aiogram bot.send)      │
                     └─────────────────────────────────────┘
```

---

## Scheduling Layer

### Why DB-Driven, Not In-Memory

PTB's `JobQueue` and APScheduler's in-memory stores lose all jobs on restart. For a production bot with per-user schedules, we need:
- Schedules persisted in the database
- Re-registration on startup
- A single polling loop that checks `next_trigger_at <= now()`

### The Scheduler Tick

A single periodic job runs every 1-5 minutes and evaluates all due schedules + event triggers:

```python
async def tick_scheduler(bot: Bot) -> None:
    """Run every minute. Find due schedules and event triggers, dispatch notifications."""
    now = datetime.now(timezone.utc)

    # 1. Find due schedules
    due_schedules = await db.get_due_schedules(now)

    for schedule in due_schedules:
        prefs = await db.get_notification_prefs(schedule.user_id)

        # Gate checks
        if not should_send(schedule, prefs, now):
            await log_execution(schedule, "skipped")
            continue

        # Dispatch
        await dispatch_notification(schedule, bot)

        # Advance to next trigger
        schedule.last_triggered_at = now
        schedule.trigger_count += 1
        schedule.next_trigger_at = compute_next_trigger(schedule)
        await db.save_schedule(schedule)

    # 2. Evaluate event triggers for active users
    active_users = await db.get_users_needing_event_check(now)
    for user_id in active_users:
        triggers = await evaluate_event_triggers(user_id)
        if triggers:
            top = triggers[0]  # highest priority
            prefs = await db.get_notification_prefs(user_id)
            if should_send_event(top, prefs, now):
                await dispatch_event_trigger(top, bot)


def should_send(schedule, prefs, now) -> bool:
    """Gate checks before sending any notification."""
    if prefs.global_pause:
        return False
    if schedule.pause_until and now < schedule.pause_until:
        return False
    if is_in_quiet_hours(now, prefs):
        return False
    if prefs.notifications_sent_today >= prefs.max_notifications_per_day:
        return False
    if schedule.schedule_type not in prefs.enabled_types:
        return False
    return True
```

### Integration with APScheduler

Since aiogram doesn't have a built-in job queue, use APScheduler's `AsyncIOScheduler` for the tick:

```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

scheduler = AsyncIOScheduler()

async def start_proactive_engine(bot: Bot) -> None:
    """Start the proactive engine alongside the bot."""
    scheduler.add_job(
        tick_scheduler,
        IntervalTrigger(minutes=1),
        kwargs={"bot": bot},
        id="proactive_tick",
        replace_existing=True,
    )
    scheduler.start()

async def stop_proactive_engine() -> None:
    scheduler.shutdown(wait=False)
```

This gives us a single system-level cron. All per-user scheduling logic lives in the DB, not in APScheduler jobs.

---

## Schedule Data Model

### Core Tables

```python
class ScheduleType(StrEnum):
    DAILY_REVIEW = "daily_review"          # Vocabulary/grammar SRS review
    QUIZ = "quiz"                          # Test on recent material
    PROGRESS_REPORT = "progress_report"    # Weekly/monthly summary
    PRACTICE_REMINDER = "practice_reminder" # Generic "time to practice"
    CUSTOM = "custom"                      # User-defined free text


class ScheduleStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    EXPIRED = "expired"
```

### UserSchedule

```python
@dataclass
class UserSchedule:
    id: str                          # UUID
    user_id: str                     # Telegram user ID
    schedule_type: ScheduleType
    status: ScheduleStatus

    # Recurrence pattern (RFC 5545 RRULE)
    # Examples:
    #   "FREQ=DAILY;BYHOUR=8;BYMINUTE=0"
    #   "FREQ=WEEKLY;BYDAY=TU,TH;BYHOUR=18;BYMINUTE=0"
    #   "FREQ=WEEKLY;INTERVAL=1;BYDAY=SU"
    rrule: str

    # Denormalized for display
    time_of_day: time | None         # User's local timezone
    days_of_week: list[str] | None   # None = every day
    user_timezone: str               # IANA timezone ("Europe/Moscow")

    # Boundaries
    starts_at: datetime | None       # UTC, None = immediately
    expires_at: datetime | None      # UTC, None = never
    pause_until: datetime | None     # UTC, for "pause until March 1st"

    # Execution tracking
    next_trigger_at: datetime        # UTC — THE hot query field
    last_triggered_at: datetime | None
    trigger_count: int

    # Metadata
    description: str                 # "Daily vocabulary review at 8am"
    custom_message: str | None       # User-provided notification text
    created_by: str                  # "user" | "system" | "onboarding"
    created_at: datetime
```

**Key DB index**: `WHERE status = 'active' ORDER BY next_trigger_at` — this is the only query the scheduler tick runs.

### Why RRULE

RFC 5545 RRULE strings handle every recurrence pattern users can request:
- Daily at 8am: `FREQ=DAILY;BYHOUR=8;BYMINUTE=0`
- Weekdays at 9am: `FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=0`
- Every 3 days: `FREQ=DAILY;INTERVAL=3;BYHOUR=10;BYMINUTE=0`
- Monthly on 1st: `FREQ=MONTHLY;BYMONTHDAY=1;BYHOUR=9;BYMINUTE=0`

`python-dateutil.rrule` parses these and computes next occurrence with DST handling:

```python
from dateutil.rrule import rrulestr
from zoneinfo import ZoneInfo

def compute_next_trigger(schedule: UserSchedule) -> datetime | None:
    tz = ZoneInfo(schedule.user_timezone)
    now_local = datetime.now(timezone.utc).astimezone(tz)
    rule = rrulestr(schedule.rrule, dtstart=schedule.created_at.replace(tzinfo=tz))
    next_local = rule.after(now_local)
    if next_local is None:
        return None
    if schedule.pause_until:
        pause_local = schedule.pause_until.astimezone(tz)
        if next_local < pause_local:
            next_local = rule.after(pause_local)
    return next_local.astimezone(timezone.utc) if next_local else None
```

### UserNotificationPreferences

```python
@dataclass
class UserNotificationPreferences:
    user_id: str
    timezone: str                           # IANA timezone
    quiet_hours_start: time | None          # In user's tz, e.g., 21:00
    quiet_hours_end: time | None            # In user's tz, e.g., 09:00
    global_pause: bool                      # Master kill switch
    global_pause_until: datetime | None     # UTC
    max_notifications_per_day: int          # Default: 3
    notifications_sent_today: int           # Reset daily
    enabled_types: list[ScheduleType]       # Which types the user allows
```

### ScheduleExecution (audit log)

```python
@dataclass
class ScheduleExecution:
    id: str
    schedule_id: str
    user_id: str
    triggered_at: datetime                  # UTC
    status: str                             # sent | skipped_quiet | skipped_paused | skipped_limit | failed
    session_id: str | None                  # Claude session ID if LLM was used
    cost: float | None                      # LLM session cost
    notification_type: str                  # "template" | "llm"
```

---

## Three Notification Tiers

Not every notification needs an LLM. Separating template vs LLM notifications is the #1 cost optimization.

### Tier 1: Template-Based (Free)

Pure Python string formatting, no Claude session. Used for:
- **Streak-at-risk** alerts ("Your 12-day streak is at risk! Quick 2-min review?")
- **Milestone celebrations** ("You've hit 400 words!")
- **Incomplete exercise nudges** ("You left 3 questions unfinished. Pick up where you stopped?")
- **Score trend notifications** ("3 sessions in a row improving!")

```python
TEMPLATES = {
    "streak_risk": [
        "Your {streak}-day streak is at risk! A quick 2-minute review will save it.",
        "Don't let {streak} days of hard work go to waste. One review is all it takes!",
        "{name}, your streak is counting on you. Just {due_count} words to review.",
    ],
    "milestone_vocab": [
        "You've hit {count} words of vocabulary! That's a huge milestone.",
    ],
    "incomplete_exercise": [
        "You left {remaining} questions unfinished on {topic}. Pick up where you stopped?",
    ],
}

def build_template_notification(trigger_type: str, user: dict, data: dict) -> str:
    import random
    templates = TEMPLATES.get(trigger_type, ["Time to practice!"])
    template = random.choice(templates)
    return template.format(name=user["name"], streak=user["streak"], **data)
```

### Tier 2: LLM-Powered ($0.05)

Runs a Claude Agent SDK session. Used for:
- **Vocabulary review sessions** (presents due cards, evaluates answers)
- **Weekly progress summaries** (analyzes scores, suggests focus areas)
- **Re-engagement after inactivity** (warm, personalized welcome back)

```python
async def run_proactive_llm_session(
    user_id: str, task_type: str, task_data: dict
) -> tuple[str, str | None, float]:
    """Run a proactive LLM session. Returns (notification_text, session_id, cost)."""
    user = await db.get_user(user_id)
    session_ctx = compute_session_context(user)
    system_prompt = build_proactive_system_prompt(user, session_ctx, task_type, task_data)

    # ... ClaudeSDKClient session with tools ...
    # Returns notification text to send via Telegram
```

### Tier 3: Smart Template + LLM Hybrid

For some notifications, generate the text once with LLM, then cache it:
- **Daily review intro**: LLM generates a personalized intro once, then reuse for 24h
- **Weekly plan**: LLM generates a weekly plan on Sunday, reference it in daily reminders

---

## Event-Driven Triggers

Beyond time-based schedules, evaluate these events periodically:

### Tier 1: Critical (check every tick)

| Event | Condition | Action | Cost |
|-------|-----------|--------|------|
| Streak at risk | `streak > 0 AND not_studied_today AND local_hour >= 20` | Template | $0.00 |
| Cards due for review | `FSRS due_count >= 5` | LLM review session | $0.05 |
| User inactive | `last_session > 48h AND was_active (streak > 3)` | LLM re-engagement | $0.05 |

### Tier 2: Learning path (check daily)

| Event | Condition | Action | Cost |
|-------|-----------|--------|------|
| Weak area persistent | `topic in weak_areas for 3+ sessions` | Template suggestion | $0.00 |
| Score trend change | `3+ consecutive improving/declining` | Template | $0.00 |
| Incomplete exercise | `last_activity.status == "incomplete" AND gap 1-24h` | Template | $0.00 |

### Tier 3: Periodic (weekly/monthly)

| Event | Condition | Action | Cost |
|-------|-----------|--------|------|
| Weekly summary | Sunday morning | LLM progress report | $0.05 |
| Monthly retrospective | 1st of month | LLM full analysis | $0.05 |
| Retention decay | `FSRS predicts >20% vocab below threshold in 3 days` | LLM batch review | $0.05 |

### Event Trigger Evaluator

```python
async def evaluate_event_triggers(user_id: str) -> list[ProactiveTrigger]:
    user = await db.get_user(user_id)
    triggers: list[ProactiveTrigger] = []
    now = datetime.now(timezone.utc)

    # Streak at risk
    if user["streak"] > 0 and not user.get("studied_today"):
        local_hour = get_user_local_hour(user)
        if local_hour >= 20:
            triggers.append(ProactiveTrigger(
                type="streak_risk", user_id=user_id,
                priority=1, requires_llm=False,
                data={"streak": user["streak"]},
            ))

    # FSRS cards due
    due_count = await db.count_due_cards(user_id)
    if due_count >= 5:
        triggers.append(ProactiveTrigger(
            type="cards_due", user_id=user_id,
            priority=1, requires_llm=True,
            data={"due_count": due_count},
        ))

    # Inactivity
    last = user.get("last_session")
    if last:
        gap = now - datetime.fromisoformat(last)
        if gap > timedelta(hours=48) and user["streak"] > 3:
            triggers.append(ProactiveTrigger(
                type="user_inactive", user_id=user_id,
                priority=2, requires_llm=True,
                data={"gap_hours": gap.total_seconds() / 3600},
            ))

    # Incomplete exercise
    activity = user.get("last_activity", {})
    if activity.get("status") == "incomplete":
        ts = activity.get("timestamp")
        if ts:
            gap = now - datetime.fromisoformat(ts)
            if timedelta(hours=1) < gap < timedelta(hours=24):
                triggers.append(ProactiveTrigger(
                    type="incomplete_exercise", user_id=user_id,
                    priority=2, requires_llm=False,
                    data={"topic": activity["topic"]},
                ))

    return sorted(triggers, key=lambda t: t.priority)
```

---

## Spaced Repetition: FSRS

Use `py-fsrs` (Free Spaced Repetition Scheduler) for vocabulary review scheduling. FSRS models the probability of recall and schedules reviews when P(recall) drops below a threshold.

### Why FSRS Over SM-2

- FSRS requires 20-40% fewer reviews than SM-2 for the same retention rate
- Models retrievability directly — can ask "what is P(recall) right now?" for any card
- Built-in JSON serialization for DB storage
- Single knob: `desired_retention` (default 0.9 = review when recall drops to 90%)

### Minimal Integration

```python
from fsrs import Scheduler, Card, Rating

scheduler = Scheduler(desired_retention=0.9)

# After user reviews a word
card = Card.from_json(db_card["fsrs_state"])
card, log = scheduler.review_card(card, Rating.Good)
await db.update_card(card_id, fsrs_state=card.to_json(), due=card.due)

# In the periodic tick, batch-check for due cards
due_cards = await db.query("SELECT * FROM vocabulary WHERE due <= $1 AND user_id = $2", now, user_id)
```

### Mapping FSRS to Notifications

Don't schedule one notification per card. Instead, the periodic tick checks:
1. How many cards are due for this user?
2. If `due_count >= threshold` (e.g., 5), send one batched review notification
3. The review session (LLM) presents all due cards in one go

This keeps costs flat: one $0.05 session regardless of whether the user has 5 or 50 due cards.

---

## User-Configurable Proactivity

### MCP Tool: `manage_schedule`

Claude parses natural language schedule requests into structured tool calls:

```python
@tool(
    "manage_schedule",
    """Create, update, pause, resume, or delete a notification schedule.

    Actions: create, update, delete, pause, resume, list

    RRULE format (RFC 5545):
    - Daily at 8am: FREQ=DAILY;BYHOUR=8;BYMINUTE=0
    - Weekdays at 9am: FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=0
    - Tue/Thu at 6pm: FREQ=WEEKLY;BYDAY=TU,TH;BYHOUR=18;BYMINUTE=0
    - Every 3 days: FREQ=DAILY;INTERVAL=3;BYHOUR=10;BYMINUTE=0
    - Monthly on 1st: FREQ=MONTHLY;BYMONTHDAY=1;BYHOUR=9;BYMINUTE=0

    For pause, set pause_until to ISO datetime.""",
    {"user_id": str, "action": str, "schedule_type": str,
     "rrule": str, "description": str, "pause_until": str, "schedule_id": str},
)
async def manage_schedule(args: dict) -> dict:
    action = args["action"]
    if action == "create":
        # Validate RRULE
        try:
            rrulestr(args["rrule"])
        except ValueError:
            return error("Invalid RRULE format")
        schedule = UserSchedule(
            id=uuid4().hex,
            user_id=args["user_id"],
            schedule_type=ScheduleType(args["schedule_type"]),
            rrule=args["rrule"],
            description=args["description"],
            # ... compute next_trigger_at ...
        )
        await db.create_schedule(schedule)
        return success(f"Created: {schedule.description}")
    elif action == "pause":
        # ...
    elif action == "list":
        schedules = await db.get_user_schedules(args["user_id"])
        return success({"schedules": [s.to_dict() for s in schedules]})
```

### Validation in `can_use_tool`

```python
async def schedule_guard(tool_name: str, tool_input: dict, context):
    if "manage_schedule" not in tool_name:
        return PermissionResultAllow()

    # Validate RRULE syntax
    if tool_input.get("action") in ("create", "update"):
        try:
            rrulestr(tool_input.get("rrule", ""))
        except (ValueError, TypeError):
            return PermissionResultDeny(message="Invalid RRULE. Please generate a valid RFC 5545 RRULE.")

    # Limit max schedules per user
    if tool_input.get("action") == "create":
        count = await db.count_user_schedules(tool_input["user_id"])
        if count >= 10:
            return PermissionResultDeny(message="Maximum 10 schedules per user.")

    return PermissionResultAllow()
```

### Natural Language Flow

```
User: "Remind me to practice every day at 8am"
  -> UserPromptSubmit hook detects scheduling intent
  -> Injects additionalContext: "USER_INTENT_DETECTED: schedule_create"
  -> Claude calls manage_schedule(action="create", schedule_type="practice_reminder",
       rrule="FREQ=DAILY;BYHOUR=8;BYMINUTE=0", description="Daily practice at 8am")
  -> can_use_tool validates RRULE
  -> Tool creates schedule, computes next_trigger_at
  -> Claude: "Done! I'll remind you to practice every day at 8:00 AM."

User: "Stop notifications until March 1st"
  -> Claude calls manage_schedule(action="pause", pause_until="2026-03-01T00:00:00")
  -> All user's schedules paused

User: "What reminders do I have?"
  -> Claude calls manage_schedule(action="list")
  -> Returns formatted list of active/paused schedules
```

### Intent Detection Patterns

Added to the existing `UserPromptSubmit` hook:

```python
SCHEDULE_PATTERNS = [
    (r"(?i)(remind|notification|schedule|alert|send me).*(every|daily|weekly|monthly)", "schedule_create"),
    (r"(?i)(stop|pause|disable|no more).*(remind|notification|schedule|alert)", "schedule_pause"),
    (r"(?i)(resume|restart|re-?enable|turn back on).*(remind|notification|schedule)", "schedule_resume"),
    (r"(?i)(quiet|silent|do not disturb|dnd|only between)", "schedule_quiet_hours"),
    (r"(?i)(on vacation|away|break|holiday).*(until|for|through)", "schedule_pause_temporal"),
    (r"(?i)(what|show|list).*(remind|notification|schedule)", "schedule_list"),
]
```

---

## Default Schedules (System-Created on Onboarding)

When a user completes onboarding, create these default schedules:

```python
async def create_default_schedules(user_id: str, timezone: str) -> None:
    """Create sensible default schedules during onboarding."""
    # Daily vocabulary review at 9am
    await db.create_schedule(UserSchedule(
        user_id=user_id,
        schedule_type=ScheduleType.DAILY_REVIEW,
        rrule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
        user_timezone=timezone,
        description="Daily vocabulary review at 9:00 AM",
        created_by="onboarding",
    ))

    # Weekly progress report on Sunday
    await db.create_schedule(UserSchedule(
        user_id=user_id,
        schedule_type=ScheduleType.PROGRESS_REPORT,
        rrule="FREQ=WEEKLY;BYDAY=SU;BYHOUR=10;BYMINUTE=0",
        user_timezone=timezone,
        description="Weekly progress report on Sunday",
        created_by="onboarding",
    ))
```

The user can later modify, pause, or delete these via natural language or inline keyboards.

---

## Proactive → Interactive Transition

From Experiment 14, we validated this pattern:

1. **Proactive session sends notification** → store notification text + session context in DB
2. **User responds in Telegram** → load stored context, inject into system prompt:
   ```
   ## CONTEXT: USER IS RESPONDING TO A NOTIFICATION
   You just sent this notification: "You have 5 words ready for review..."
   The user is responding. Don't repeat the notification — pick up from there.
   ```
3. **Bot continues seamlessly** — no repetition, jumps straight into the activity

This is already proven in Exp 14 Test D. No session resume needed.

---

## Cost Model

### Per-Notification Cost

| Notification Type | Method | Cost |
|-------------------|--------|------|
| Streak saver | Template | $0.00 |
| Milestone celebration | Template | $0.00 |
| Incomplete exercise nudge | Template | $0.00 |
| Score trend alert | Template | $0.00 |
| Vocabulary review session | LLM | $0.05 |
| Weekly progress summary | LLM | $0.05 |
| Re-engagement message | LLM | $0.05 |

### Daily Cost Per User (Typical)

| Component | Frequency | Cost |
|-----------|-----------|------|
| Morning review (LLM) | 1x/day | $0.05 |
| Streak saver (template) | 0-1x/day | $0.00 |
| Event triggers (template) | 0-2x/day | $0.00 |
| Weekly summary (LLM) | 1/7 days | ~$0.007 |
| **Daily average** | | **~$0.06** |

### Monthly Projection

| Scale | Monthly Cost |
|-------|-------------|
| 1 user | $1.80 |
| 100 users | $180 |
| 1000 users | $1,800 |

This is 2-3x cheaper than the naive Exp 12 estimate ($4,136 for 1000 users) by replacing 2 of 3 daily LLM sessions with templates.

---

## Timezone Handling

### Principles

1. **Store everything in UTC** — all `next_trigger_at`, `triggered_at`, `created_at`
2. **Use `zoneinfo` (stdlib)** — Python 3.12+, no pytz dependency
3. **IANA timezone names** — "Europe/Moscow", never "MSK"
4. **DST-aware scheduling** — `dateutil.rrule` handles transitions correctly when given timezone-aware `dtstart`

### Detecting User Timezone

Ask during onboarding via inline keyboard:

```
What's your timezone?
[Europe/Moscow] [Europe/London] [Europe/Berlin]
[America/New_York] [America/Chicago] [America/Los_Angeles]
[Asia/Tokyo] [Asia/Shanghai] [Asia/Kolkata]
[Other...]
```

Or let Claude ask naturally during conversation: "What timezone are you in?"

---

## Integration with Existing Architecture

### New Components

| Component | Type | Purpose |
|-----------|------|---------|
| `proactive_engine.py` | Module | Scheduler tick, event triggers, notification dispatcher |
| `schedule_manager.py` | Module | CRUD for UserSchedule, RRULE parsing, next-trigger computation |
| `notification_templates.py` | Module | Template-based notification builder |
| `fsrs_engine.py` | Module | FSRS card management, due-card queries |
| `manage_schedule` tool | MCP Tool | User-facing schedule management via Claude |

### New Dependencies

| Package | Purpose |
|---------|---------|
| `apscheduler >=3.11,<4.0` | System-level periodic tick (1 job, not per-user) |
| `python-dateutil` | RRULE parsing and next-occurrence computation |
| `fsrs` | Spaced repetition scheduling for vocabulary |

### System Prompt Extension

Add to `build_system_prompt()`:

```
## NOTIFICATION PREFERENCES
Active schedules: Daily review at 9:00 AM, Weekly summary on Sundays
Quiet hours: 9:00 PM - 9:00 AM (Europe/Moscow)
Timezone: Europe/Moscow

## SCHEDULING INSTRUCTIONS
When the user asks to set reminders or change notification preferences,
use the manage_schedule tool. Parse their request into an RRULE.
```

### Post-Session Pipeline Extension

After existing `post_session_pipeline`:

```python
# Update FSRS cards if vocabulary was reviewed
await update_fsrs_cards(user_id, session_results)

# Check for new event triggers
await mark_studied_today(user_id)
await check_milestones(user_id)
```

---

## Summary

### Key Design Decisions

1. **DB-driven scheduling** with single periodic tick — not per-user in-memory jobs
2. **RRULE (RFC 5545)** for recurrence patterns — expressive, standard, DST-safe
3. **Three notification tiers** — template (free), LLM ($0.05), hybrid (cached LLM)
4. **Event-driven triggers** — evaluated in the periodic tick, not as separate crons
5. **FSRS for spaced repetition** — modern, efficient, built-in JSON serialization
6. **Natural language schedule management** — Claude parses via `manage_schedule` tool
7. **Rate limiting** — max 3 notifications/day, quiet hours, per-type opt-out
8. **Proactive → interactive continuity** — store notification text in DB, inject as context (Exp 14 validated)

### Anti-Patterns to Avoid

- **Don't schedule one APScheduler job per user** — DB-driven tick scales better
- **Don't use LLM for every notification** — templates for simple alerts save 2-3x cost
- **Don't schedule one notification per FSRS card** — batch due cards into single sessions
- **Don't rely on in-memory job stores** — they don't survive restarts
- **Don't send more than 2-3 notifications/day** — notification fatigue kills engagement
