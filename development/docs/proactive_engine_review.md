# Proactive Engine Review — Feb 2026

## Files reviewed
- `src/adaptive_lang_study_bot/proactive/dispatcher.py` (443 lines)
- `src/adaptive_lang_study_bot/proactive/tick.py` (347 lines)
- `src/adaptive_lang_study_bot/proactive/triggers.py` (414 lines)
- `src/adaptive_lang_study_bot/proactive/admin_reports.py` (358 lines)

## Architecture assessment

The proactive engine is well-architected: bounded concurrency, distributed locking,
atomic dedup via Redis SET NX, paginated user loading, and clean separation between
trigger evaluation (pure Python) and dispatch (I/O). No rewrite needed.

## Issues found and fixes applied

### 1. Dead code: `_DEFAULT_NOTIF_PREFS` (dispatcher.py:85-91)
- Dict defined but never referenced anywhere. Removed.

### 2. Verbose CTA keyboard builder (dispatcher.py:94-125)
- 32-line if/elif chain replaced with data-driven `_CTA_MAPPINGS` dict.

### 3. Quiet hours check inlined in should_send() (dispatcher.py:167-176)
- 10-line overnight logic extracted to `_is_in_quiet_hours(user, local_now)`.

### 4. Duplicated schedule advance logic (tick.py)
- Three places computed next_trigger from RRULE + updated schedule.
- Extracted `_advance_schedule(schedule_id, rrule, user_tz, success)` helper.

### 5. Magic constants scattered across modules
- 16 constants in triggers.py (lines 9-25) + 2 in tick.py (lines 30-31)
  + 4 in admin_reports.py (lines 31-34) moved to `config.py:BotTuning`.
- Enables runtime tuning without redeployment.

## Not changed (acceptable as-is)

### dispatch_notification() is 238 lines
- Long but linear transactional orchestration: dedup → LLM quota → render → send → record.
- Steps are tightly coupled (rollback on failure), so splitting into helpers would
  scatter the state machine across functions without improving readability.
- Each step has clear comments and error handling. Acceptable.

### _process_one() closure in tick.py
- Reduced from 81 to ~40 lines after extracting _advance_schedule().
- Remaining logic is straightforward: skip check → build trigger → dispatch → advance.

### Trigger functions in triggers.py
- Each is 15-30 lines with clear return conditions. Well-structured.
- `make_trigger()` factory provides consistent shape. Good.

### Pool check duplication in admin_reports.py
- Two nearly-identical pool check functions (interactive/proactive).
- Could extract a helper but each is only 15 lines. Low priority.

## Mapping dict validation (future)
- `_TRIGGER_TO_SESSION_TYPE` and `_TRIGGER_TO_NOTIF_CATEGORY` in dispatcher.py
  have no startup validation that keys match ALL_TRIGGERS.
- If a new trigger is added to triggers.py but not to these dicts, it silently
  falls back to defaults. Consider adding a startup assertion in bot/app.py.
