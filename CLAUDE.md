# CLAUDE.md

## Project

Adaptive language study bot — a personalized, proactive AI language tutor on Telegram. Uses `claude-agent-sdk` to run Claude as the teaching agent, with PostgreSQL, Redis, and aiogram. Single-process bot with embedded proactive notification engine and a separate Gradio admin panel.

## Environment

- **Python**: 3.12+ (strict)
- **Package manager**: Poetry. Always use `poetry run` to execute anything.
- **Infrastructure**: PostgreSQL 16, Redis 7, Docker Compose.

```bash
poetry install                                 # install deps
poetry run python <path>                       # run any script
poetry run pytest                              # all tests
poetry run pytest tests/unit/                  # unit only (no DB/Redis/SDK)
poetry run pytest tests/integration/           # needs Docker (testcontainers)
poetry run pytest tests/llm/                   # needs ANTHROPIC_API_KEY
poetry run pytest -x                           # stop on first failure
poetry add <package>                           # add dependency
```

## Directory Structure

```
src/adaptive_lang_study_bot/
├── config.py              # Settings (pydantic-settings), BotTuning (centralized magic numbers), TIER_LIMITS
├── enums.py               # StrEnum: UserTier, SessionType, NotificationTier, CloseReason, Difficulty, SessionStyle, etc.
├── utils.py               # Helpers: user_local_now, safe_zoneinfo, compute_next_trigger, get_language_name, strip_mcp_prefix, is_user_admin, compute_new_streak, summarize_tool_usage
├── i18n.py                # t(key, lang, **kwargs) with JSON locale fallback
├── logging_config.py      # Loguru setup + stdlib bridge, runtime log level toggle
├── metrics.py             # Prometheus counters/gauges/histograms (16 metrics)
├── locales/               # JSON locale files (en, ru, es, fr, de, pt, it)
├── agent/
│   ├── tools.py           # 10 MCP tools, _SESSION_TYPE_TOOLS, _USER_MUTABLE_FIELDS
│   ├── hooks.py           # PostToolUse (adaptive hints), UserPromptSubmit (turn limit), Stop hooks
│   ├── prompt_builder.py  # build_system_prompt(), build_proactive_prompt(), compute_session_context()
│   ├── session_manager.py # SessionManager (interactive) + run_proactive_llm_session() + run_summary_llm_session() (standalone)
│   └── pool.py            # SessionPool: asyncio.Semaphore (500 interactive, 50 proactive)
├── bot/
│   ├── app.py             # Bot + Dispatcher setup, middleware/router registration, startup/shutdown
│   ├── helpers.py         # get_user_lang, safe_edit_text, build_filterable_keyboard, split_agent_sections
│   ├── middlewares/       # DBSession → Auth → RateLimit (order matters)
│   └── routers/           # start, chat, settings, review, stats, debug
├── db/
│   ├── engine.py          # SQLAlchemy async engine + session factory
│   ├── models.py          # 7 ORM models: User, Vocabulary, Session, Schedule, ExerciseResult, Notification, VocabularyReviewLog
│   ├── repositories.py    # Static-method repos with explicit AsyncSession param
│   └── migrations/        # Alembic
├── cache/
│   ├── client.py          # Redis pool
│   ├── keys.py            # All Redis key patterns + TTL constants
│   ├── redis_lock.py      # Distributed lock helpers (Lua release)
│   └── session_lock.py    # Per-user session lock (SET NX)
├── proactive/
│   ├── tick.py            # APScheduler 60s tick: Phase 1 (schedules) → Phase 2 (event triggers)
│   ├── triggers.py        # 10 event triggers in ALL_TRIGGERS priority list
│   ├── dispatcher.py      # should_send() gates, template/LLM/hybrid dispatch, CTA keyboards
│   └── admin_reports.py   # Stats reports (12h), health alerts (60s), 7 health checks
├── fsrs_engine/scheduler.py  # FSRS spaced repetition wrapper
├── pipeline/post_session.py  # Post-session pipeline (pure Python, no LLM): validation, streak, difficulty, milestones
├── admin/app.py              # Gradio admin panel (5 tabs: Users, Sessions, Costs, Alerts, System)
└── entrypoints/              # run_bot.py (bot + APScheduler), run_admin.py (Gradio)

tests/
├── unit/       # No DB, no Redis, no SDK — pure logic, constants, security invariants
├── integration/# testcontainers (Postgres + Redis) — repos, cache, constraints, atomic ops
└── llm/        # Real Claude API calls — prompt compliance, security, tool calling, session types

development/
├── code_sandbox/  # SDK experiment scripts (exp_01..exp_14) + shared.py
└── docs/          # Design docs: experiment_observations, personalization, proactive_behavior, user_paths
```

`src/` layout: configured in pyproject.toml `packages = [{include = "adaptive_lang_study_bot", from = "src"}]`. Only the installed package is importable.

## System Design

### Two paths into the agent

**Interactive** (user-initiated): Telegram message → aiogram middlewares → `chat` router → `SessionManager.handle_message()` → `ClaudeSDKClient.query()` → response back to Telegram. One long-lived session per user, reused across messages until closed.

**Proactive** (system-initiated): APScheduler 60s tick → `tick_scheduler()` evaluates due schedules (Phase 1) and event triggers (Phase 2) → `dispatcher.dispatch_notification()` → either renders an i18n template ($0) or runs a short-lived `run_proactive_llm_session()` → sends Telegram message with CTA keyboard. Proactive sessions are standalone (not managed by `SessionManager`), have no hooks, 30s timeout, haiku model, max 10 turns.

### Agent session architecture

Each `ClaudeSDKClient` instance spawns a Claude CLI subprocess. Sessions are assembled from four layers:

1. **System prompt** (`prompt_builder.py`) — full string override, built fresh per session from a DB snapshot of the user's profile. Up to 14 sections for interactive (role, rules, output format, tool requirements, student profile, first session guide, teaching approach, level guidance, exercise types, vocab strategy, session context, comeback adaptation, scheduling, bot capabilities). Some are conditional: first session guide (new users only), level guidance, vocab strategy, comeback adaptation (gap ≥ 48h). Proactive prompts are compact (5 sections: role+rules, profile, time context, task, trigger context).

2. **MCP tools** (`tools.py`) — 10 tools registered via `@tool` + `create_sdk_mcp_server()`. Each tool is a closure capturing `(session_factory, user_id)` — creates its own short-lived DB session per call. Tools are filtered by `_SESSION_TYPE_TOOLS[session_type]` before MCP server creation — the SDK never sees disallowed tools.

3. **Hooks** (`hooks.py`) — per-session `SessionHookState` tracks exercise scores, tool calls, turn count. `PostToolUse` injects adaptive difficulty hints after exercises (cheap behavior steering — no extra LLM call). `UserPromptSubmit` injects wrap-up hint at 80% of turn limit.

4. **Config** — model, max turns, thinking (adaptive), effort (low), cost limits — all tier-dependent from `TIER_LIMITS`.

### Two-tier system

Free vs premium tiers (admin-assigned, no billing). Defined in `config.py:TIER_LIMITS` as `dict[UserTier, TierLimits]`. Affects: model (haiku/sonnet), turn limits (20/35), cost caps (per-session and daily), idle timeout (5/10 min), thinking (adaptive for both), effort (low), notification limits (2/8 LLM/day), rate limits (5/20 msg/min).

### Concurrency model

- **One session per user** — Redis SET NX lock with tier-based TTL (`cache/session_lock.py`)
- **Global pool** — `SessionPool` with two `asyncio.Semaphore`s: 500 interactive, 50 proactive (`agent/pool.py`)
- **Idle cleanup** — `SessionManager` background loop (30s interval) closes sessions after tier-specific timeout, sends warning at 70%
- **Proactive tick lock** — Redis distributed lock prevents concurrent tick evaluation across hypothetical multi-process deployments
- **Rate limiting** — Redis-based per-user, enforced in middleware

### Database (PostgreSQL)

7 tables, SQLAlchemy 2.0 async ORM. `users.telegram_id` (BIGINT) as PK.

Key patterns:
- **Repositories** — static methods, explicit `AsyncSession` param, no hidden state. Handlers use middleware-injected `db_session`; tool closures create their own sessions via `session_factory`.
- **Atomic array ops** — `append_score`, `check_and_increment_notification` use PostgreSQL array operations and IS DISTINCT FROM guards to prevent races.
- **Post-session atomic UPDATE** — `post_session.py` collects all field changes into a single UPDATE to avoid overwriting concurrent tool modifications.
- **FSRS denormalized** — `vocabulary.fsrs_due` column enables efficient due-card queries without deserializing FSRS state.
- **Schedules** — RRULE strings (RFC 5545), `next_trigger_at` is the hot polled field for Phase 1 of proactive tick.

### Cache (Redis)

Redis is NOT used as a traditional cache — it provides distributed coordination:

| Purpose | Key pattern (see `cache/keys.py`) | Mechanism |
|---------|-----------------------------------|-----------|
| Session lock (one per user) | `session:active:{user_id}` | SET NX + owner token + Lua release |
| Rate limiting | `ratelimit:user:{user_id}:minute` | Increment with TTL |
| Notification dedup | `notif:dedup:{user_id}:{type}:{date}` | SET with daily TTL |
| LLM notification counter | `notif:llm_count:{user_id}:{date}` | Increment with daily TTL |
| Notification cooldown | `notif:cooldown:{user_id}` | SET with 5-min TTL |
| Proactive tick lock | `lock:proactive_tick` | Distributed lock |
| Admin health/stats locks | `lock:admin_health`, `lock:admin_stats_report` | Distributed lock |
| Admin alert dedup | `admin:alert:{type}:{date_hour}` | 1-hour cooldown |

### Proactive engine

10 event triggers in `triggers.py:ALL_TRIGGERS`, evaluated in priority order. Only one fires per user per tick. Phase 2 skips users with active interactive sessions to avoid mid-session interruptions. Three tiers of triggers: Tier 1 (engagement — streak risk, due cards, inactivity), Tier 1.5 (re-engagement — post-onboarding escalation windows, lapsed user escalation, dormant weekly), Tier 2 (learning — weak areas, score trends, incomplete exercises).

Notifications dispatch via three tiers: **template** (i18n, $0), **LLM** (`run_proactive_llm_session()`), **hybrid** (try LLM, fall back to template). `should_send()` gates: paused → preference → quiet hours → cooldown → dedup. LLM notifications exceeding daily limit downgrade to template.

### Guardrail layers

```
Config (tier limits) → System prompt (rules) → Tool filtering (per session type)
→ Tool validation (whitelists, caps) → Hooks (adaptive hints, turn warnings)
→ Post-session pipeline (profile integrity, difficulty auto-adjust)
```

Each layer is independent — if one is bypassed, others still enforce correctness.

## Code Style

**Imports**: All at file top. No inline/lazy imports (except `development/code_sandbox/`).

**Type hints**: Modern Python 3.12+ syntax everywhere:
```python
def process(items: list[str]) -> dict[str, int]: ...
def get_user(user_id: int) -> User | None: ...
```

**Async**: Async-first for all I/O.

**Naming**: Files `snake_case.py`, classes `PascalCase`, functions/vars `snake_case`, constants `UPPER_SNAKE_CASE`, private `_leading_underscore`.

**Errors**: Never suppress silently — log or re-raise.

## Stateful Objects & Lifecycle

The bot is a single async process. All stateful objects are module-level singletons initialized at import time or on first use. Lifecycle is managed via `on_startup`/`on_shutdown` in `bot/app.py`.

| Object | Module | Init | Shutdown | Notes |
|--------|--------|------|----------|-------|
| `settings` | `config.py` | Import time (pydantic-settings, reads env) | — | Immutable `Settings` instance |
| `tuning` | `config.py` | Import time | — | Immutable `BotTuning` frozen dataclass, centralized magic numbers |
| `engine` | `db/engine.py` | Import time (`create_async_engine`) | `dispose_engine()` | SQLAlchemy async engine, pool_size=150, max_overflow=100, pool_timeout=10s |
| `async_session_factory` | `db/engine.py` | Import time (`async_sessionmaker`) | — (engine disposes pool) | Factory callable; `expire_on_commit=False` |
| `_redis` | `cache/client.py` | Lazy on first `get_redis()` call, thread-safe via `asyncio.Lock` | `close_redis()` | `Redis` + `ConnectionPool` pair; all cache modules call `get_redis()` |
| `session_pool` | `agent/pool.py` | Import time | — (semaphores don't need cleanup) | Two `asyncio.Semaphore`s (500 interactive, 50 proactive) + counters |
| `session_manager` | `agent/session_manager.py` | Import time (empty) | `session_manager.stop()` | Owns `_sessions: dict[int, ManagedSession]`, started via `.start(bot)` which launches `_cleanup_loop` |
| `_scheduler` | `fsrs_engine/scheduler.py` | Import time | — | FSRS `Scheduler()` instance, stateless evaluator |
| Prometheus metrics | `metrics.py` | Import time (module-level `Counter`/`Gauge`/`Histogram`) | — | 16 metrics, HTTP server started in `on_startup` |
| `_handler_id` | `logging_config.py` | `configure_logging()` in entrypoint | — | Loguru handler ID for runtime log level toggle |
| APScheduler | `entrypoints/run_bot.py` | `main()` creates `AsyncIOScheduler` | `scheduler.shutdown()` in finally block | 3 jobs: proactive tick (60s), stats report (12h), health alerts (60s) |
| `Bot` / `Dispatcher` | `bot/app.py` | `create_bot()` / `create_dispatcher()` in entrypoint | `bot.session.close()` | aiogram objects, created fresh per run |

### Per-user transient state

`ManagedSession` (dataclass in `session_manager.py`) — one per active user, lives in `session_manager._sessions[user_id]`. Created when user sends first message, destroyed on close. Contains:
- `client: ClaudeSDKClient` — the SDK subprocess (managed via `AsyncExitStack`)
- `hook_state: SessionHookState` — accumulates exercise scores, tool calls, turn count within session
- `lock: asyncio.Lock` — serializes message processing for this user
- `lock_token: str` — Redis session lock owner token for ownership-verified refresh/release
- Cost, turn count, warning flags (`limit_warned`, `cost_warned`, `idle_warned` — one-shot bools)

Session tools and hooks are **closures** — not objects. Created per session in `create_session_tools()` / `build_session_hooks()`, they capture `(session_factory, user_id)` and are garbage-collected when `ManagedSession` is destroyed.

### Startup sequence (`run_bot.py` → `bot/app.py`)

1. `configure_logging()` — set up loguru
2. `create_bot()` / `create_dispatcher()` — aiogram setup (middlewares + routers registered)
3. `AsyncIOScheduler` created + 3 jobs added (tick, stats, health)
4. `scheduler.start()` — begins proactive tick loop
5. `dp.start_polling(bot)` → triggers `on_startup`:
   - `start_metrics_server()` — Prometheus HTTP endpoint
   - `session_manager.start(bot)` — starts `_cleanup_loop` background task

### Shutdown sequence

1. `on_shutdown`:
   - `session_manager.stop()` — closes all active `ManagedSession`s (SDK subprocesses killed, Redis locks released, pool slots freed, DB sessions updated)
   - `close_redis()` — closes Redis pool
   - `dispose_engine()` — closes SQLAlchemy connection pool
2. `scheduler.shutdown(wait=True)` — stops APScheduler
3. `bot.session.close()` — closes aiohttp session

### DB session ownership

Two distinct patterns for obtaining DB sessions:
- **Handlers** (routers): use middleware-injected `db_session` from `DBSessionMiddleware`. Never call `async_session_factory()` directly.
- **Tool closures + background tasks** (agent tools, post-session pipeline, proactive tick, admin reports): call `async with async_session_factory() as db:` — each opens a short-lived session from the pool.

## SDK Usage

The bot uses `claude-agent-sdk` (not `claude-code-sdk`, not raw `anthropic`).

```python
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    AssistantMessage, ResultMessage, TextBlock, ToolUseBlock,
    HookMatcher, tool, create_sdk_mcp_server,
)
```

SDK spawns Claude CLI as subprocess. Nesting guard removed at import time in `session_manager.py`: `os.environ.pop("CLAUDECODE", None)`.

### Three types of SDK sessions

| Type | Entry point | Model | Hooks | Tools | Timeout | Notes |
|------|-------------|-------|-------|-------|---------|-------|
| Interactive | `SessionManager._create_session()` | Tier-based (haiku/sonnet) | PostToolUse + UserPromptSubmit + Stop | 6-9 (session-type filtered) | Idle timeout (5/10 min) | Long-lived, multi-turn, reused across messages |
| Proactive | `run_proactive_llm_session()` | haiku (`tuning.proactive_model`) | None | 2-4 (session-type filtered) | 30s hard timeout | Standalone function, single query, must call `send_notification` once |
| Summary | `run_summary_llm_session()` | haiku (`tuning.proactive_model`) | None | None (tool-less) | 15s timeout, max 3 turns | Generates session-end summary, no DB writes |

### SDK client lifecycle (interactive)

```
acquire pool slot → acquire Redis lock → build system prompt → create DB session record
→ create_session_tools() (closures) → filter tools by session type → build_session_hooks()
→ create_langbot_server(filtered_tools) → ClaudeAgentOptions(system_prompt, hooks, mcp_servers, ...)
→ ClaudeSDKClient(options) → enter via AsyncExitStack → store as ManagedSession
... (multi-turn: query → receive_response → TextBlock/ToolUseBlock/ResultMessage) ...
→ on close: exit_stack.aclose() (kills subprocess) → release Redis lock → release pool slot
→ update DB session → asyncio.create_task(run_post_session(...))
```

### Tool return format

All tools return SDK-expected dicts:
```python
{"content": [{"type": "text", "text": "<json>"}]}              # success
{"content": [{"type": "text", "text": "<msg>"}], "is_error": True}  # error
```

### Hook injection mechanism

Hooks steer the agent cheaply by returning `additionalContext` that becomes part of the conversation:
```python
return {
    "continue_": True,
    "hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": "ADAPTIVE_HINT: Student is struggling...",
    },
}
```

### System prompt structure

**Interactive** (`build_system_prompt`) — up to 14 sections: ROLE, RULES, OUTPUT FORMAT, TOOL REQUIREMENTS, STUDENT PROFILE, FIRST SESSION GUIDE (conditional, new users only — added alongside teaching approach, not replacing it), TEACHING APPROACH, LEVEL GUIDANCE (per-CEFR, conditional), EXERCISE TYPES, VOCABULARY STRATEGY (conditional), SESSION CONTEXT (greeting style, last activity, session history, topic performance, celebrations, notification reply context), COMEBACK ADAPTATION (conditional, gap ≥ 48h), SCHEDULING INSTRUCTIONS, BOT CAPABILITIES.

**Proactive** (`build_proactive_prompt`) — 5 sections: ROLE+RULES, STUDENT PROFILE (compact), TIME CONTEXT, TASK (per-session-type instructions from `_PROACTIVE_TASK_INSTRUCTIONS`), TRIGGER CONTEXT. Agent must call `send_notification` exactly once.

**Summary** (`build_summary_prompt`) — 4 sections: ROLE, RULES (close-reason-aware tone), SESSION DATA (exercise counts, topics, words, duration), TASK (progress vs no-progress branch).

SDK exception types: `CLINotFoundError`, `ProcessError`, `ClaudeSDKError`.

## Critical Invariants

These rules MUST be maintained when modifying code:

**Middleware order** (`bot/app.py`): `DBSessionMiddleware → AuthMiddleware → RateLimitMiddleware`. Handlers receive `db_session: AsyncSession` and `user: User` from middleware. Never create sessions via `async_session_factory()` in handlers.

**Callback data prefixes** — routers use distinct prefixes to avoid conflicts:
- `start.py`: `native:`, `target:`, `tz:`, `level:`, `goal:`, `goaldone:`, `interest:`, `interestdone:`, `schedpref:`, `back:`, `moretarget:`, `moretz:`, `cta:`
- `settings.py`: `set:`, `setval:`, `setlvl:`, `setntp:`, `stz:`, `setlang:`, `setlangconfirm:`, `deletemeconfirm`, `sched:pause:`, `sched:resume:`, `sched:del:`, `sched:cdel:`, `setqh:`, `setmn:`, `morelang_s:`, `moretz_s:`
- `review.py`: `fsrs:`
- Never reuse a prefix across routers.

**Security boundaries** (in `tools.py`):
- `_USER_MUTABLE_FIELDS`: only `{interests, learning_goals, preferred_difficulty, session_style, topics_to_avoid, notifications_paused, additional_notes}` can be modified via `update_preference`
- `_SESSION_TYPE_TOOLS`: defines which tools each session type can access — disallowed tools are excluded from MCP server entirely
- `send_notification` only available in proactive session types
- Array caps enforced: interests ≤ 8, learning_goals ≤ 5, topics_to_avoid ≤ 5, additional_notes ≤ 10, weak/strong areas ≤ 10

**One session per user**: Redis SET NX lock. `SessionManager` for interactive, `run_proactive_llm_session()` as standalone function for proactive.

**Post-session pipeline** (`pipeline/post_session.py`): Runs as `asyncio.Task` after every session close. Collects all user field changes into a single atomic `UPDATE` to avoid overwriting concurrent modifications.

**Tuning constants**: All magic numbers are centralized in `config.py:BotTuning` dataclass. Use `tuning.<field>` instead of hardcoding values.

## Internationalization

All user-facing strings are localized via `i18n.t(key, lang, **kwargs)`.

- 7 supported native languages: en, ru, es, fr, de, pt, it (`SUPPORTED_NATIVE_LANGUAGES` frozenset)
- Locale files: `locales/{lang}.json` — flat key-value JSON
- Fallback chain: requested lang → English → raw key
- List-valued keys: random variant selection (for notification template variety)
- `reload_locales()` to clear LRU cache in tests

**Key prefix conventions**: `start.*`, `help.*`, `chat.*`, `settings.*`, `deleteme.*`, `review.*`, `stats.*`, `rate_limit.*`, `session.*` (lifecycle), `cta.*` (button labels), `pipeline.*` (milestones), `notif.*` (notification templates — list-valued), `lang.*` (language names).

## Database

7 tables in `db/models.py`. PK for `users` is `telegram_id` (BIGINT). All FKs use `ondelete="CASCADE"` or `ondelete="SET NULL"`.

Repositories (`db/repositories.py`) use static methods with explicit `AsyncSession` parameter — no hidden state.

Key design choices (read code for details):
- FSRS state denormalized in vocabulary (`fsrs_due` column) for efficient due-card queries
- Schedules use RRULE strings; `next_trigger_at` is the polled field
- Notification preferences inlined in users table (avoids JOIN on proactive tick)

## Sensitive Files

Never commit: `.env`, `development/code_sandbox/output/`.
