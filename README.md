# Adaptive Language Study Bot

A personalized AI language tutor that runs on Telegram. The bot adapts exercises to each user's level, tracks vocabulary with spaced repetition (FSRS), and sends proactive study reminders on user-configured schedules.

Powered by Claude (via `claude-agent-sdk`), PostgreSQL, Redis, and aiogram. Most of the codebase was developed with [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (Opus 4.6).

Try the live bot: [@personal_lang_study_bot](https://t.me/personal_lang_study_bot)

## Features

- **Adaptive exercises** — the AI generates exercises tailored to the user's level (A1-C2), interests, weak areas, and preferred difficulty. No static exercise bank.
- **Vocabulary tracking** — words are stored per-user with FSRS spaced repetition. The bot schedules reviews at optimal intervals.
- **Proactive notifications** — streak-at-risk alerts, vocabulary review reminders, weekly progress summaries. Users control schedules via natural language or `/settings`.
- **Two-tier system** — Free (Haiku 4.5) and Premium (Sonnet 4.6) with different limits. No billing — admin grants premium via the Gradio panel.
- **17 target languages** — English, French, Spanish, Italian, German, Portuguese, Russian, Chinese, Japanese, Korean, Arabic, Turkish, Dutch, Polish, Swedish, Ukrainian, Hindi. Users can learn any of these.
- **7 UI languages** — English, Russian, Spanish, French, German, Portuguese, Italian. All bot UI, notifications, and session messages are rendered in the user's native language via an i18n system with JSON locale files.
- **Gradio admin panel** — monitor users, sessions, costs, alerts, and system health. Gradio auth required (`ADMIN_API_TOKEN`).
- **Health alerts** — automated Telegram alerts to admins for cost spikes, pool saturation, pipeline failures, and connectivity issues.

## Requirements

- Python 3.12+
- PostgreSQL 16
- Redis 7
- Claude CLI (installed on the host or in Docker)
- Poetry (for local development)

## Quick Start (Docker)

1. Clone the repository and create the `.env` file with the required variables:

```env
# Required
TELEGRAM_BOT_TOKEN=your-telegram-bot-token
ANTHROPIC_API_KEY=your-anthropic-api-key
POSTGRES_PASSWORD=your-secure-password
ADMIN_API_TOKEN=your-admin-token  # Gradio admin panel refuses to start without it

# Optional (defaults shown)
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_USER=langbot
POSTGRES_DB=langbot
REDIS_URL=redis://localhost:6379/0
MAX_CONCURRENT_INTERACTIVE_SESSIONS=50
MAX_CONCURRENT_PROACTIVE_SESSIONS=10
PROACTIVE_TICK_INTERVAL_SECONDS=60
ADMIN_HOST=0.0.0.0
ADMIN_PORT=7860
ADMIN_TELEGRAM_IDS=[]         # JSON array of admin user IDs, e.g. [123456,789012]
LOG_LEVEL=INFO
METRICS_PORT=9090
DB_POOL_SIZE=30
DB_MAX_OVERFLOW=40
DB_POOL_RECYCLE=3600          # seconds
REDIS_MAX_CONNECTIONS=50
```

2. Start all services:

```bash
docker compose up -d
```

This starts 4 containers:
- **bot** — Telegram bot + APScheduler proactive engine (single process)
- **admin** — Gradio admin panel on port 7860
- **postgres** — PostgreSQL 16
- **redis** — Redis 7 (128MB max, LRU eviction)

3. Run database migrations:

```bash
docker compose exec bot python -m alembic upgrade head
```

4. Open the admin panel at `http://localhost:7860`.

## Quick Start (Local Development)

```bash
# Install dependencies
poetry install

# Set up .env with your keys (see above)

# Start PostgreSQL and Redis (or use Docker for just these)
docker compose up -d postgres redis

# Run database migrations
poetry run alembic upgrade head

# Start the bot
poetry run python -m adaptive_lang_study_bot.entrypoints.run_bot

# Start the admin panel (separate terminal)
poetry run python -m adaptive_lang_study_bot.entrypoints.run_admin
```

## Configuration

All configuration is via environment variables (loaded by `pydantic-settings` from `.env`). See `src/adaptive_lang_study_bot/config.py` for all options.

### Tier Limits

| Parameter | Free | Premium |
|-----------|------|---------|
| Model | claude-haiku-4-5 | claude-sonnet-4-6 |
| Max turns/session | 20 | 30 |
| Max sessions/day | 5 | unlimited |
| Session idle timeout | 5 min | 10 min |
| Thinking mode | disabled | adaptive (3000 budget tokens) |
| LLM notifications/day | 2 | 8 |
| Rate limit | 8 msg/min | 20 msg/min |
| Max cost/session | $0.20 | $1.50 |
| Max cost/day | $0.50 | $8.00 |

To grant a user premium access, use the admin panel or update the `tier` column in the `users` table directly.

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/start` | Onboarding: select native language, target language, timezone |
| `/review` | Start a vocabulary review session (FSRS due cards) |
| `/stats` | View progress: level, streak, vocabulary count, recent scores |
| `/settings` | Manage preferences, schedules, quiet hours, timezone, target language |
| `/end` | End the current study session explicitly |
| `/deleteme` | Delete account and all associated data (with confirmation) |
| `/help` | Command reference |
| `/debug` | Toggle per-message debug output (admin-only) |

Any other text message starts or continues an interactive study session with the AI tutor.

## Architecture

### System Design

```
┌─────────────────────────────────────────────────────────────────────────┐
│                            Bot Process                                  │
│                                                                         │
│  ┌──────────────┐    ┌─────────────────────────────────────────────┐    │
│  │   Telegram    │    │              Interactive Path                │    │
│  │     API       │    │                                             │    │
│  │              ◄├───►│  aiogram Dispatcher                         │    │
│  │  (polling)    │    │    │                                        │    │
│  │               │    │    ▼                                        │    │
│  │               │    │  Middlewares: DBSession → Auth → RateLimit  │    │
│  │               │    │    │                                        │    │
│  │               │    │    ▼                                        │    │
│  │               │    │  Routers: start│chat│settings│review│stats  │    │
│  │               │    │               │                             │    │
│  │               │    │               ▼                             │    │
│  │               │    │         SessionManager                      │    │
│  │               │    │               │                             │    │
│  │               │    │               ▼                             │    │
│  │               │    │  ┌──── Agent Session ────────────────────┐  │    │
│  │               │    │  │  ClaudeSDKClient (CLI subprocess)     │  │    │
│  │               │    │  │    ├── System Prompt (13 sections)    │  │    │
│  │               │    │  │    ├── MCP Server (11 tools) ────────►├──├───►│
│  │               │    │  │    └── Hooks (PostToolUse,            │  │    │
│  │               │    │  │         UserPromptSubmit, Stop)       │  │    │
│  │               │    │  └──────────────┬───────────────────────┘  │    │
│  │               │    │                 │ on close                  │    │
│  │               │    │                 ▼                           │    │
│  │               │    │  Post-Session Pipeline                     │    │
│  │               │    │  (streak, difficulty, milestones) ────────►├───►│
│  │               │    └─────────────────────────────────────────────┘    │
│  │               │                                                      │
│  │               │    ┌─────────────────────────────────────────────┐    │
│  │               │    │              Proactive Path                  │    │
│  │               │    │                                             │    │
│  │  ◄────────────├────│  APScheduler (60s tick)                     │    │
│  │  (template)   │    │    │                                        │    │
│  │               │    │    ▼                                        │    │
│  │               │    │  10 Event Triggers (priority-ordered)       │    │
│  │               │    │    │                                        │    │
│  │               │    │    ▼                                        │    │
│  │               │    │  Dispatcher (template / LLM / hybrid) ────►├───►│
│  │               │    └─────────────────────────────────────────────┘    │
│  │               │                                                      │
│  │  ◄────────────├──── Health Alerts + Stats Reports (to admins)        │
│  └──────────────┘                                                       │
│                        Prometheus metrics :9090                          │
└──────────┬──────────────────────────┬───────────────────────────────────┘
           │                          │
           ▼                          ▼
┌─────────────────────┐   ┌─────────────────────────┐   ┌────────────────┐
│    PostgreSQL 16     │   │        Redis 7           │   │  Anthropic API │
│    (7 tables)        │   │  session locks           │   │                │
│                      │   │  rate limits             │   │  Haiku 4.5     │
│  users, vocabulary,  │   │  notification dedup      │   │  Sonnet 4.6    │
│  sessions, schedules,│   │  proactive tick lock     │   │                │
│  exercises, notifs,  │   │  admin alert dedup       │   │  (via Claude   │
│  review_log          │   │                          │   │   Agent SDK)   │
└──────────▲──────────┘   └─────────────────────────┘   └────────────────┘
           │
┌──────────┴──────────┐
│   Gradio Admin       │
│   Panel :7860        │
│                      │
│  Users │ Sessions    │
│  Costs │ Alerts      │
│  System              │
└─────────────────────┘
```

### Directory Structure

```
src/adaptive_lang_study_bot/
├── config.py              # Settings, tier limits
├── enums.py               # StrEnum constants (UserTier, SessionType, NotificationTier, CloseReason, etc.)
├── utils.py               # Shared helpers (language names, streak, tool summaries)
├── i18n.py                # Internationalization: t(key, lang) with locale JSON fallback
├── locales/               # JSON locale files (en, ru, es, fr, de, pt, it)
├── agent/                 # Claude SDK: tools, hooks, prompt, session manager
├── bot/                   # aiogram: middlewares, routers, app setup
│   └── routers/           # start, chat, settings, review, stats, debug
├── db/                    # SQLAlchemy models, repositories, migrations
├── cache/                 # Redis: session lock, rate limits, key patterns, distributed locks
├── proactive/             # APScheduler tick, triggers, dispatcher, admin reports
├── fsrs_engine/           # FSRS spaced repetition wrapper
├── pipeline/              # Post-session validation (pure Python)
├── admin/                 # Gradio admin panel
└── entrypoints/           # run_bot.py, run_admin.py
```

### Key design decisions

- **Per-session closures** — each agent session creates its own tool and hook functions. Tools capture a session factory and user ID (each tool call gets its own short-lived DB session). This ensures complete data isolation between concurrent users.
- **Long-lived sessions** — each user gets one `ClaudeSDKClient` that persists across messages until closed (by turn/cost limit, idle timeout, or `/end`). A new session builds a fresh system prompt from the user's current DB profile snapshot.
- **Hybrid personalization** — the system prompt carries the user profile snapshot (read-only context), while MCP tools handle all DB writes (exercises, vocabulary, preferences).
- **Three-tier notifications** — template ($0 cost, random variant from locale files), LLM (short-lived proactive session generates personalized message), or hybrid (try LLM, fall back to template). Free users get 2 LLM notifications/day, premium get 8.
- **Re-engagement triggers** — escalating nudge system for post-onboarding (24h → 3d → 7d → 14d), lapsed users (gentle → compelling → miss-you), and dormant users (weekly nudges for 15-45 days inactive), with automatic stop after final attempt.
- **Localized UI** — all user-facing messages (bot UI, notifications, session summaries, warnings) rendered via `i18n.t()` in the user's native language.
- **Post-session pipeline** — after each session, a pure-Python pipeline validates data integrity, updates streaks, auto-adjusts difficulty, and detects milestones.

### Database

PostgreSQL with 7 tables:

| Table | Purpose |
|-------|---------|
| `users` | User profiles, preferences, notification settings, tier |
| `vocabulary` | Per-user words with FSRS spaced repetition state |
| `sessions` | Session records with cost/token tracking |
| `schedules` | RRULE-based recurring schedules (daily review, weekly summary, etc.) |
| `exercise_results` | Individual exercise scores for analytics |
| `notifications` | Audit log of all sent/skipped notifications |
| `vocabulary_review_log` | Individual FSRS review events |

### Redis

Used for locking, rate limiting, and deduplication:

| Key pattern | TTL | Purpose |
|-------------|-----|---------|
| `session:active:{id}` | 7-12 min | Track active sessions, enforce one-per-user |
| `ratelimit:user:{id}:*` | 60s | Per-user rate limiting |
| `notif:dedup:{user_id}:{type}:{date}` | 24h | Prevent duplicate notifications |
| `notif:llm_count:{user_id}:{date}` | 24h | Track daily LLM notification count per user |
| `lock:proactive_tick` | 5 min | Distributed lock for tick scheduler |
| `lock:admin_stats_report` | 5 min | Distributed lock for stats report |
| `lock:admin_health` | 2 min | Distributed lock for health alerts |
| `admin:alert:{type}:{hour}` | 1h | Dedup health alerts per type per hour |

## Database Migrations

```bash
# Create a new migration
poetry run alembic revision --autogenerate -m "description"

# Apply migrations
poetry run alembic upgrade head

# Rollback one step
poetry run alembic downgrade -1
```

## Testing

```bash
# Run all tests
poetry run pytest

# Run only unit tests
poetry run pytest tests/unit/

# Run only integration tests (requires Docker for testcontainers)
poetry run pytest tests/integration/

# Run specific file
poetry run pytest tests/unit/agent/test_tools.py

# Stop on first failure
poetry run pytest -x
```

### Unit tests (`tests/unit/`)

No database, Redis, or SDK required. They verify:
- Tool constants and session-type permissions
- Security invariants (mutable field whitelists)
- Pure functions (message splitting, timezone conversion, language detection, utils helpers)
- Prompt builder output structure and sanitization
- Notification template rendering via i18n
- Trigger evaluation logic (all 10 triggers + re-engagement escalation)
- Dispatcher gate logic (should_send conditions)
- Post-session pipeline steps and post-session logic
- FSRS engine operations
- Config validation
- Hook behavior (adaptive hints, wrap-up injection)
- Auth middleware logic
- Admin role checks
- Quiet hours edge cases
- Difficulty adjustment
- i18n translation and fallback chains
- Proactive LLM session lifecycle

### Integration tests (`tests/integration/`)

Require Docker. Use `testcontainers` to spin up real PostgreSQL 16 and Redis 7 containers. Tests cover:
- CASCADE and FK constraints
- Repository operations (user, vocabulary, session, schedule, exercise, notification)
- Redis session lock and cache behavior
- Atomic operations and concurrent access
- Notification dispatch integration

### LLM tests (`tests/llm/`)

Require `ANTHROPIC_API_KEY`. Make real Claude API calls via `claude-agent-sdk` to test:
- System prompt compliance (native language, off-topic refusal)
- Security boundaries (tool permissions, field whitelists)
- Tool calling compliance
- Session type behavior (onboarding vs interactive)
- Adaptive behavior (score-based hints)
- Multi-turn conversations

## Monitoring

### Prometheus Metrics

The bot exposes a Prometheus metrics HTTP endpoint on port `METRICS_PORT` (default 9090). 17 metrics are tracked across 3 types:

- **Gauges** — active session pool size (interactive/proactive)
- **Counters** — sessions created/closed, messages processed, errors, notifications sent/skipped, proactive ticks, pipeline completions
- **Histograms** — session cost, session duration, message cost, tick duration, pipeline duration, notification LLM cost

### APScheduler Jobs

The bot process runs 3 background jobs:

| Job | Interval | Purpose |
|-----|----------|---------|
| `proactive_tick` | 60s | Schedule-based and event-triggered notifications |
| `health_alerts` | 60s | Evaluate 7 health conditions, alert admins via Telegram |
| `admin_stats_report` | 12h | Send usage/cost summary to admins via Telegram |

### Health Alerts

Automated alerts sent to admin Telegram IDs (deduped per hour per type):

| Alert | Condition |
|-------|-----------|
| Cost spike | Today's cost > 2x 7-day daily average |
| Interactive pool high | > 80% capacity |
| Proactive pool high | > 80% capacity |
| Pipeline failures | > 3 failures in last hour |
| Redis unhealthy | Connection failed |
| DB unhealthy | Connection failed |
| Notification failures | > 30% failure rate (min 5 total in last hour) |

### Admin Panel (Gradio)

Available at `http://localhost:7860` (requires `ADMIN_API_TOKEN`, login as `admin`). Five tabs:
- **Users** — search by name/username/ID, view full profile, toggle tier (free/premium), toggle active status, toggle admin role
- **Sessions** — recent 100 sessions: user, type, cost, turns, tools used, pipeline status, duration
- **Costs** — summary (today/7d/30d), daily breakdown (14 days), per-user cost ranking (7 days)
- **Alerts** — pipeline failures, notification delivery stats (7-day status breakdown + recent 20)
- **System** — active session pool, Redis memory, DB status, configuration snapshot, health alert status (7 checks)

### Logs

The bot uses `loguru` for structured logging. All significant events are logged:
- User registration and profile updates
- Session creation, tool calls, and completion
- Proactive tick execution and notification dispatch
- Post-session pipeline results
- Health alerts and admin reports

## Cost Estimates

Estimates assume prompt caching is active (system prompt + tool definitions cached across turns). Pricing: Haiku 4.5 — $1/$5 per MTok (input/output), Sonnet 4.6 — $3/$15 per MTok. Proactive notifications use Haiku regardless of tier.

| Tier | Avg session cost | Daily (1-2 sessions) | Monthly (1 user) |
|------|-----------------|---------------------|------------------|
| Free (Haiku 4.5) | $0.03-0.08 | ~$0.10 | ~$3 |
| Premium (Sonnet 4.6) | $0.20-0.50 | ~$0.80 | ~$25 |

Scale estimates assume 60% daily active rate and 1.5 sessions per active user per day:

| Scale | All Free | Mixed (90/10) | All Premium |
|-------|----------|---------------|-------------|
| 100 users | ~$180/mo | ~$320/mo | ~$1,500/mo |
| 1,000 users | ~$1,800/mo | ~$3,200/mo | ~$15,000/mo |

## Docker Compose Operations

The stack runs 4 services:

| Service | Image | Purpose | Port |
|---------|-------|---------|------|
| `bot` | custom (Dockerfile) | Telegram bot + APScheduler proactive engine | 9090 (Prometheus metrics) |
| `admin` | custom (Dockerfile) | Gradio admin panel | 7860 |
| `postgres` | postgres:16-alpine | Database (max_connections=200) | 5432 (internal) |
| `redis` | redis:7-alpine | Locks, rate limits, dedup (128MB, LRU) | 6379 (internal) |

### Lifecycle

```bash
# Start all services (detached)
docker compose up -d

# Stop all services (preserves data volumes)
docker compose down

# Stop and remove all data (fresh start)
docker compose down -v

# Rebuild after code changes and restart
docker compose up -d --build

# Rebuild only one service
docker compose up -d --build bot

# Restart a single service (no rebuild)
docker compose restart bot

# Check status
docker compose ps
```

### Logs

```bash
# Follow all logs (Ctrl+C to stop)
docker compose logs -f

# Follow a single service
docker compose logs -f bot
docker compose logs -f admin
docker compose logs -f postgres
docker compose logs -f redis

# Last N lines
docker compose logs --tail 50 bot

# Last N lines + follow
docker compose logs -f --tail 100 bot

# Logs since a timestamp
docker compose logs --since "2025-01-15T10:00:00" bot

# Logs from the last hour
docker compose logs --since 1h bot

# Show timestamps
docker compose logs -t bot

# Multiple services at once
docker compose logs -f bot admin

# Grep for specific patterns (combine with standard tools)
docker compose logs bot 2>&1 | grep "ERROR"
docker compose logs bot 2>&1 | grep "user 123456"
```

### Database (PostgreSQL)

```bash
# Open psql shell
docker compose exec postgres psql -U langbot

# Run a single SQL query
docker compose exec postgres psql -U langbot -c "SELECT count(*) FROM users;"

# Run migrations (after code update)
docker compose exec bot python -m alembic upgrade head

# Check current migration version
docker compose exec bot python -m alembic current

# Create a new migration
docker compose exec bot python -m alembic revision --autogenerate -m "description"

# Rollback one migration
docker compose exec bot python -m alembic downgrade -1

# Dump the database (backup)
docker compose exec postgres pg_dump -U langbot langbot > backup_$(date +%Y%m%d_%H%M%S).sql

# Restore from backup (stop bot/admin first)
docker compose stop bot admin
docker compose exec -T postgres psql -U langbot langbot < backup_20250115_120000.sql
docker compose start bot admin
```

### Redis

```bash
# Open redis-cli
docker compose exec redis redis-cli

# Check connectivity
docker compose exec redis redis-cli ping

# View memory usage
docker compose exec redis redis-cli info memory

# List active session locks
docker compose exec redis redis-cli KEYS "session:active:*"

# List all keys (use cautiously in production)
docker compose exec redis redis-cli KEYS "*"

# Check a specific key's TTL
docker compose exec redis redis-cli TTL "session:active:123456"

# Flush all Redis data (resets locks, rate limits, dedup)
docker compose exec redis redis-cli FLUSHALL

# Monitor commands in real time (Ctrl+C to stop)
docker compose exec redis redis-cli MONITOR
```

### Shell access

```bash
# Shell into the bot container
docker compose exec bot bash

# Shell into postgres container
docker compose exec postgres sh

# Run a Python one-liner inside the bot container
docker compose exec bot python -c "from adaptive_lang_study_bot.config import settings; print(settings.model_dump_json(indent=2))"
```

### Update workflow

After pulling new code or making changes:

```bash
# 1. Rebuild and restart (zero-downtime for DB/Redis)
docker compose up -d --build

# 2. Run any new migrations
docker compose exec bot python -m alembic upgrade head

# 3. Verify services are healthy
docker compose ps
docker compose logs --tail 20 bot
```

For changes that require a fresh database:

```bash
# 1. Stop everything and remove volumes
docker compose down -v

# 2. Rebuild and start
docker compose up -d --build

# 3. Run all migrations on the empty database
docker compose exec bot python -m alembic upgrade head
```

### Resource monitoring

```bash
# CPU and memory usage per container
docker compose stats

# Disk usage (images, containers, volumes)
docker system df
```

## Maintenance

### Granting premium tier

Via admin panel or directly:
```sql
UPDATE users SET tier = 'premium' WHERE telegram_id = 123456789;
```

### Granting admin role

Via admin panel (auto-upgrades to premium) or directly:
```sql
UPDATE users SET is_admin = true, tier = 'premium' WHERE telegram_id = 123456789;
```

### Resetting a user's streak

```sql
UPDATE users SET streak_days = 0, streak_updated_at = NULL WHERE telegram_id = 123456789;
```

### Pausing all notifications for a user

```sql
UPDATE users SET notifications_paused = true WHERE telegram_id = 123456789;
```

### Viewing active sessions

Check Redis:
```bash
redis-cli KEYS "session:active:*"
```

Or via the admin panel's System tab.

### Docker resource requirements

The bot container is allocated 8GB memory to support up to 50 concurrent agent sessions (~100MB each for the Claude CLI subprocess). Redis is capped at 128MB with LRU eviction.

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Bot not responding | Check `docker compose logs bot` for errors. Verify `TELEGRAM_BOT_TOKEN` is correct. |
| "Claude Code cannot be launched inside another session" | Unset `CLAUDECODE` env var. Only happens when running inside VS Code with Claude Code extension. |
| High costs | Check the admin panel Costs tab. Consider lowering `max_sessions_per_day` or `max_cost_per_day_usd` in `config.py`. |
| Notifications not sending | Check user's `notifications_paused`, `quiet_hours_start/end`, and `max_notifications_per_day`. Check `notifications` table for `skipped_*` statuses. |
| Migrations fail | Ensure PostgreSQL is running and `POSTGRES_*` env vars are correct. Check `docker compose logs postgres`. |
| Redis connection refused | Verify `REDIS_URL` matches the Redis container hostname. Inside Docker, use `redis://redis:6379/0`. |
| Health alerts not arriving | Ensure `ADMIN_TELEGRAM_IDS` is set and the listed users have `is_admin = true` in the database. |
