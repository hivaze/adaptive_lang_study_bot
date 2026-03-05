# User Paths & UX Analysis

Comprehensive map of every user journey through the bot: what users can do, what they cannot do, and how each path solves their language-learning task.

---

## Table of Contents

1. [First Contact & Registration](#1-first-contact--registration)
2. [Onboarding Flow](#2-onboarding-flow)
3. [Core Learning Session](#3-core-learning-session)
4. [Vocabulary Review (FSRS)](#4-vocabulary-review-fsrs)
5. [Progress & Stats](#5-progress--stats)
6. [Settings & Preferences](#6-settings--preferences)
7. [Proactive Notifications](#7-proactive-notifications)
8. [Session Lifecycle & Limits](#8-session-lifecycle--limits)
9. [Account Management](#9-account-management)
10. [Admin Paths](#10-admin-paths)
11. [What Users Cannot Do](#11-what-users-cannot-do)
12. [Edge Cases & Error States](#12-edge-cases--error-states)

---

## 1. First Contact & Registration

### Trigger
User sends any message or /start to the bot for the first time.

### What Happens (invisible to user)
1. **AuthMiddleware** intercepts the event
2. No existing user found in DB → auto-creates a new `User` record
3. Native language is auto-detected from Telegram's `language_code` (e.g. `ru`, `pt-BR` → `pt`)
4. If the detected language isn't in the 7 supported UI languages, defaults to English
5. User is created with `onboarding_completed=False`, empty `target_language`

### User Experience
- User sends first message → gets redirected to onboarding
- If user sends any text (not /start), they see: "Complete setup first" message
- Only /start and /help commands work before onboarding is completed

### How It Solves the Task
Auto-registration with zero friction — no sign-up forms, no passwords. The bot meets the user where they already are (Telegram) and immediately starts the setup process.

---

## 2. Onboarding Flow

### Entry Point
`/start` command (first time or resumed)

### Steps (6-step wizard with inline keyboards)

```
Step 1: Native Language        Step 2: Target Language       Step 3: Timezone
┌──────────────────┐          ┌──────────────────────┐      ┌───────────────────┐
│ English          │    →     │ Popular (8):         │  →   │ Popular (6):      │
│ Русский          │          │ English, French,     │      │ UTC, Moscow,      │
│ Español          │          │ Spanish, German...   │      │ London, NY...     │
│ Français         │          │ [More languages]     │      │ [More timezones]  │
│ Deutsch          │          │ [← Back]             │      │ [← Back]          │
│ Português        │          └──────────────────────┘      └───────────────────┘
│ Italiano         │
└──────────────────┘

Step 4: Learning Goal          Step 5: Interests              Step 6: Schedule Pref
┌──────────────────┐          ┌──────────────────────┐      ┌───────────────────┐
│ Travel           │    →     │ ☐ Food   ☐ Music    │  →   │ Morning (9:00)    │
│ Work             │          │ ☐ Sports ☐ Tech     │      │ Afternoon (14:00) │
│ Exams            │          │ ☐ Travel ☐ News     │      │ Evening (19:00)   │
│ Hobby            │          │ [Done] [Skip]        │      │ No schedule       │
│ [Skip]           │          │ [← Back]             │      │ [← Back]          │
│ [← Back]         │          └──────────────────────┘      └───────────────────┘
└──────────────────┘
```

### What the User Can Do
- **Navigate freely**: every step has a "Back" button to return to the previous step
- **Skip optional steps**: goals (step 4) and interests (step 5) can be skipped
- **Multi-select interests**: toggle multiple interests on/off with checkmarks
- **Resume later**: if the user abandons mid-onboarding and returns, `/start` resumes from the last completed step (tracked via `milestones.onboarding_step`)
- **See more options**: target languages and timezones show popular subset first, with "More" button to expand
- **Choose same language**: can select native language as target (marked as "strengthen")

### What the User Cannot Do
- Skip native language, target language, or timezone (required for core functionality)
- Go through onboarding again after completion (`/start` shows "welcome back" message)
- Use any other bot commands until onboarding is complete

### What Happens on Completion
1. `onboarding_completed` flag set to `True`
2. If user chose a schedule preference (not "none"):
   - **Daily review schedule** created (RRULE-based, triggers at chosen hour)
   - **Weekly progress report** created (Sundays, 1 hour after chosen time, LLM-generated)
3. Onboarding tracking data cleaned from milestones
4. User gets a completion message with next steps

### How It Solves the Task
Collects the minimum information needed for personalized teaching. Each piece of data directly feeds the AI agent's system prompt:
- **Native language** → bot UI language + agent communication language
- **Target language** → determines what to teach
- **Timezone** → correct scheduling, streak tracking, greeting context
- **Goal** → shapes exercise types and vocabulary themes
- **Interests** → personalizes content (exercises about food/music/etc.)
- **Schedule** → enables proactive daily reviews without user action

---

## 3. Core Learning Session

### Entry Point
Any text message sent after onboarding is complete.

### Full Path

```
User sends text
  │
  ├─ Onboarding not complete? → "Complete setup first" (blocked)
  │
  ├─ Rate limited? → "Too many messages, please wait" (blocked)
  │
  ├─ Existing session?
  │   ├─ Turn/cost limit hit? → Close old session + summary → create new
  │   └─ Yes → Forward message to existing agent
  │
  ├─ No existing session → Create new:
  │   ├─ Daily session limit reached? → "You've used N sessions today" (blocked)
  │   ├─ Daily cost limit reached? → "Daily cost limit reached" (blocked)
  │   ├─ No pool slots? → "Bot is busy, try again later" (blocked)
  │   ├─ Redis lock failed? → "Bot is busy" (blocked)
  │   └─ Success → Agent session created
  │
  └─ Agent processes message → Response sent back
```

### What the User Can Do
- **Free-form conversation**: type anything in any language — the agent adapts
- **Request specific exercises**: "Give me a vocabulary quiz about food"
- **Ask grammar questions**: "Explain the subjunctive mood in Spanish"
- **Practice conversation**: roleplay scenarios with the agent
- **Learn vocabulary**: new words are automatically saved with FSRS cards
- **Get exercises scored**: the agent records scores (1-10) that track progress
- **Ask to change preferences**: "Make exercises harder" → agent uses `update_preference` tool
- **Manage schedules**: "Set a daily reminder at 8am" → agent uses `manage_schedule` tool
- **End session explicitly**: `/end` command closes the current session

### What the User Cannot Do
- Send non-text content (photos, voice, stickers → "Only text messages are supported")
- Have multiple concurrent sessions (one session per user, enforced by Redis lock)
- Talk about non-language topics (agent is instructed to stay on-topic)
- See the system prompt (agent refuses to reveal it)
- Change their own level directly (agent-assessed via learning plan completion)
- Change their tier (admin-assigned only)
- Bypass rate limits (8 msg/min free, 20 msg/min premium)

### Agent Behavior During Session
The agent adapts based on the full user profile snapshot in its system prompt:

| Context Signal | Agent Adaptation |
|---|---|
| Gap since last session | Greeting style (continuation / warm welcome / long absence) |
| Recent scores avg ≤ 4 | Simplifies exercises, more encouragement |
| Recent scores avg ≥ 8.5 | Increases difficulty, more challenging content |
| Session style = casual | Relaxed pace, humor, cultural tidbits |
| Session style = structured | Clear segments, grammar explanations, numbered exercises |
| Session style = intensive | Maximum density, minimal chat, rapid switching |
| Difficulty = easy | Simpler vocab, more hints, multiple-choice |
| Difficulty = hard | Complex structures, advanced vocab, minimal hints |
| Due vocab cards ≥ 3 | Suggests starting with vocabulary review |
| Weak areas present | Focuses exercises on struggling topics |
| Pending celebrations | Celebrates milestones at session start |
| Absence ≥ 48h | No guilt, warm welcome, review-first approach |
| Absence ≥ 72h | Overdue vocab, difficulty override, warm-up exercises |
| Replying to notification | Acknowledges the notification context |

### Typing Indicator
While the agent is thinking (SDK subprocess startup + model response), the bot shows a "typing..." indicator that refreshes every 4 seconds.

### Message Delivery
- Agent responses can be long → split at paragraph boundaries (max 4096 chars per Telegram message)
- HTML tag balance preserved across splits (open tags closed, reopened in next chunk)
- If Telegram rejects HTML formatting → falls back to plain text
- If agent returns nothing (only tool calls) → "I processed your request" fallback

### How It Solves the Task
This is the core value proposition: a personalized AI tutor that:
- Remembers everything about the student (level, interests, weak areas, goals)
- Adapts exercises in real-time (via hooks that inject difficulty hints after each scored exercise)
- Automatically tracks vocabulary and exercise progress (via MCP tools)
- Manages spaced repetition without manual input
- Speaks in the student's native language for explanations

---

## 4. Vocabulary Review (FSRS)

### Entry Points
1. `/review` command
2. "Start review" CTA button on notifications

### Flow

```
/review or CTA button
  │
  ├─ Not onboarded? → "Complete setup first"
  │
  ├─ No vocabulary at all? → "You haven't learned any words yet"
  │
  ├─ No cards due? → "All X words reviewed! Nothing due right now"
  │
  └─ Cards due → Show first card (front):
      ┌────────────────────────┐
      │ Card 1/12              │
      │ bonjour                │
      │ Topic: greetings       │
      │ Try to recall...       │
      │                        │
      │ [Show Answer]          │
      └────────────────────────┘
            │
            ▼ (tap Show Answer)
      ┌────────────────────────┐
      │ Card 1/12              │
      │ bonjour                │
      │ Translation: hello     │
      │ Bonjour, comment...    │
      │ Rate your recall:      │
      │                        │
      │ [Again][Hard][Good][Easy]
      └────────────────────────┘
            │
            ├─ Rate → FSRS schedules next review
            ├─ More cards? → Show next card front
            └─ No more? → "Session complete!"
                ├─ More became due? → "N more cards due" + [Review more] button
                └─ All done → final message
```

### What the User Can Do
- **Self-rate recall quality**: 4 options (Again / Hard / Good / Easy)
- **Review at their own pace**: no timer, no pressure
- **See word context**: translation and example sentence shown on card back
- **Continue reviewing**: if more cards became due during the session, offered to continue
- **Review from notification**: CTA button on "cards due" notifications directly opens review

### What the User Cannot Do
- Edit vocabulary cards from the review UI (done by the agent during sessions)
- Skip a card without rating it
- Review cards for someone else (ownership verified)
- Rate a card twice (stale button guard via `fsrs_last_review` timestamp)
- Rate with invalid values (validated: must be 1-4)

### FSRS Mechanics (invisible to user)
- **Rating 1 (Again)**: card re-queued for very soon (may appear again in same session)
- **Rating 2 (Hard)**: short interval
- **Rating 3 (Good)**: normal interval
- **Rating 4 (Easy)**: long interval
- Stability and difficulty parameters updated per card
- `fsrs_due` timestamp updated for efficient due-card queries
- Review logged in `vocabulary_review_log` for analytics
- Row-level locking prevents double-processing on rapid taps

### How It Solves the Task
Spaced repetition is the most evidence-backed method for long-term vocabulary retention. The FSRS algorithm optimally schedules reviews based on individual card difficulty and user performance, ensuring efficient use of study time.

---

## 5. Progress & Stats

### Entry Point
`/stats` command

### What the User Sees

```
📊 Your French Progress
├─ Level: B1
├─ Streak: 12 days
├─ Vocabulary: 156 words
├─ Sessions: 34
├─ Cards due: 8
├─ Difficulty: normal
├─ Style: structured
├─ Recent scores: 7, 8, 6, 9, 7
├─ Average: 7.4
├─ Goals:
│   1. Travel to France
│   2. Pass DELF B1
├─ Weak areas: subjunctive mood, irregular verbs
├─ Strong areas: greetings, food vocabulary
├─ Recent exercises:
│   • Past tense: 8/10 (translation)
│   • Articles: 6/10 (fill-in-blank)
├─ Tier: free
├─ Sessions per day: 5
└─ Messages per session: 20
```

### What the User Can Do
- See a complete snapshot of their learning progress
- Understand their tier limits
- Identify areas needing improvement (weak areas, low scores)

### What the User Cannot Do
- Modify any stats directly (all are computed from actual learning activity)
- See other users' stats
- Export stats

### How It Solves the Task
Gives the student a clear picture of where they stand, what needs work, and what they're doing well. The tier information helps them understand usage limits.

---

## 6. Settings & Preferences

### Entry Point
`/settings` command

### Main Settings Menu

```
⚙️ Settings
├─ Language: Русский → French
├─ Difficulty: normal
├─ Style: structured
├─ Timezone: Europe/Moscow
├─ Notifications: Active
├─ Max notifications: 3/day
├─ Quiet hours: 22:00 – 07:00
│
├─ [Difficulty] [Style]          ← Learning preferences
├─ [🔔 Notifications On/Off]     ← Global toggle
├─ [Notification types] [Quiet hours]
├─ [Max notifications] [Schedules]
├─ [Timezone] [Change language]
└─ (all buttons navigate to sub-menus with [← Back])
```

### Settings Sub-Flows

#### 6a. Difficulty Picker
```
Current difficulty: normal
[Easy] [Normal] [Hard]
[← Back]
```
- Changes take effect immediately (closes active session so next message uses new setting)

#### 6b. Style Picker
```
Current style: structured
[Casual] [Structured] [Intensive]
[← Back]
```
- Same behavior as difficulty

#### 6c. Notification Toggle
- Single tap toggles `notifications_paused` on/off
- When paused: ALL notifications suppressed (schedules + event triggers)

#### 6d. Notification Type Preferences
```
Notification Types
✓ Streak reminders
✓ Vocabulary reviews
✓ Progress reports
✗ Re-engagement
✓ Learning nudges
[← Back]
```
- Toggle individual categories on/off
- Disabled categories silently skip (no DB record for the skip)

#### 6e. Quiet Hours
```
Current: 22:00 – 07:00
[22:00–07:00 Night]
[23:00–08:00 Late night]
[21:00–09:00 Evening]
... (8 presets)
[Disable quiet hours]
[← Back]
```
- No custom time input — preset options only
- Overnight quiet hours supported (e.g. 22:00–07:00)

#### 6f. Max Notifications Per Day
```
Current: 3/day
[1] [2] [3] [4]
[5] [8] [10]
[← Back]
```

#### 6g. Schedules Management
```
Your Schedules:
  • Daily review at 9:00 — Next: 2026-02-24 09:00
  • Weekly report (paused) — Next: N/A
  [⏸ Pause "Daily review..."] [🗑 Delete]
  [▶ Resume "Weekly report..."] [🗑 Delete]
[← Back]
```
- Pause/resume individual schedules
- Delete with confirmation dialog
- Times shown in user's local timezone

#### 6h. Timezone Change
- Same picker as onboarding (popular + "More")
- Changes recalculate all schedule triggers
- Closes active session

#### 6i. Target Language Switch
```
Current: French
[English] [Spanish] [German] ... [More]
[← Back]
    │
    ▼ (select new language)
⚠️ Switching to Spanish will:
  - Delete 156 vocabulary words
  - Delete 234 exercise results
  - Reset level from B1 to A1
  - Reset streak from 12 to 0
[Yes, switch] [Cancel]
```
- **Destructive action** with explicit confirmation showing exact data counts
- Wraps all deletions in a savepoint (atomic: all or nothing)
- Preserves: preferences (difficulty, style), schedules, notification settings, timezone

### What the User Can Do
- Control every aspect of their learning experience
- Fine-tune notification behavior (global toggle, per-type, quiet hours, daily cap)
- Manage schedules created by onboarding or by the agent
- Switch languages (with full data reset warning)
- Navigate freely (every sub-menu has Back button)

### What the User Cannot Do
- Change their native language after onboarding (must delete account and re-register)
- Change their tier (admin-only)
- Change their level directly (agent-assessed via adjust_level tool after plan assessment)
- Set custom quiet hours (presets only)
- Create schedules from settings UI (only via agent or onboarding)
- See cost data or admin controls

### How It Solves the Task
Full user agency over their learning experience. Students can adjust difficulty when the auto-adjustment doesn't match their preference, manage notification frequency to avoid fatigue, and change languages when they want to learn something new.

---

## 7. Proactive Notifications

### Overview
Notifications are sent TO the user without user action. Two sources:

### 7a. Schedule-Based Notifications (Phase 1)
Created during onboarding or by the agent via `manage_schedule` tool.

| Schedule Type | Default Timing | Content |
|---|---|---|
| Daily Review | User's chosen hour | "You have N cards due for review" |
| Weekly Progress Report | Sunday, +1h from daily | LLM-generated summary of week's progress |
| Quiz | Agent-created | "Quick quiz time!" |
| Practice Reminder | Agent-created | "Time to practice!" |

### 7b. Event-Triggered Notifications (Phase 2)
Evaluated every 60 seconds for all active users.

| Trigger | When | Message Style |
|---|---|---|
| **Streak risk** | Evening (18:00+), hasn't studied today, streak > 0 | Template: "Don't lose your N-day streak!" |
| **Cards due** | 5+ due cards, inactive 2+ hours | Template: "You have N cards waiting for review" |
| **User inactive** | Streak ≥ 3, inactive 48+ hours | LLM-generated personalized nudge |
| **Post-onboarding 24h** | Onboarded but no sessions, 20-48h | Template: "Ready to start learning?" |
| **Post-onboarding 3d** | Same, 48-72h | Template: escalated message |
| **Post-onboarding 7d** | Same, 72-168h | LLM: final personalized attempt |
| **Post-onboarding 14d** | Same, 168-336h | Template: last-chance nudge |
| **Lapsed gentle** | Had sessions, inactive 2-4 days | Template: gentle reminder |
| **Lapsed compelling** | Inactive 4-8 days | Template: mentions progress stats |
| **Lapsed miss you** | Inactive 8-15 days | LLM: personalized with interests |
| **Dormant weekly** | 15-45 days inactive | Template: fires ~weekly |
| **Weak area persistent** | Weak area across 3+ sessions | Template: "Still struggling with X" |
| **Weak area drill due** | Weak area + inactive 12h+ | Template: "Time for a targeted drill" |
| **Score trend** | 3+ scores improving or declining | Template: trend observation |
| **Incomplete exercise** | Last session incomplete, 1-24h ago | Template: "Want to continue?" |

### Notification Delivery Path

```
Trigger fires
  │
  ├─ Notifications paused? → skip (silent)
  ├─ Category disabled in preferences? → skip (silent)
  ├─ Quiet hours active? → skip (silent)
  ├─ Already sent this type today? (Redis dedup) → skip
  ├─ Daily notification limit reached? (DB atomic check) → skip + record
  │
  ├─ LLM tier + free user + LLM limit reached? → downgrade to template
  │
  ├─ Render message:
  │   ├─ Template tier → i18n locale template (random variant)
  │   └─ LLM tier → short proactive SDK session (haiku, 30s timeout)
  │
  ├─ Attach CTA keyboard:
  │   ├─ cards_due → [Start review] → opens /review flow
  │   ├─ streak_risk → [Quick session] → prompts to start chatting
  │   ├─ incomplete → [Continue] → prompts to resume
  │   ├─ lapsed/dormant → [Resume learning] → prompts to start chatting
  │   └─ everything else → [Start a session] → prompts to start chatting
  │
  └─ Send via Telegram
      ├─ Success → record in DB, update user's last_notification
      ├─ User blocked bot → deactivate user account
      └─ Other failure → release dedup slot for retry next tick
```

### What the User Receives
- A Telegram message with motivational/informational text
- An inline keyboard button (CTA) for immediate action
- Tapping the CTA either starts a review or prompts a session

### What the User Can Control
- **Global toggle**: pause all notifications in /settings
- **Per-type toggle**: disable specific categories (streaks, vocab, progress, re-engagement, nudges)
- **Quiet hours**: preset time windows with no notifications
- **Max per day**: cap from 1 to 10
- **Schedule management**: pause, resume, or delete individual schedules

### What the User Cannot Control
- Exact trigger conditions (e.g., can't change the 18:00 streak risk threshold)
- LLM vs template tier (determined by user tier and limits)
- Specific notification wording (templates have random variants)

### Re-Engagement Escalation (invisible to user)
For users who complete onboarding but never start a session:
```
20-48h → gentle template nudge
48-72h → slightly more compelling template
72-168h → LLM-generated personalized message
168-336h → final template attempt
>336h → stop nudging forever
```

For users who had sessions but went silent:
```
2-4 days → gentle template reminder
4-8 days → template mentioning their progress stats
8-15 days → LLM with interests, vocabulary count, personalization
15-45 days → periodic weekly template
>45 days → stop nudging forever
```

### How It Solves the Task
Proactive notifications solve the "forgetting to study" problem — the #1 reason language learners drop off. The graduated escalation system respects user boundaries (quiet hours, preferences, daily caps) while keeping learners engaged through multiple channels:
- **Streaks**: loss aversion psychology
- **Due cards**: FSRS-optimal review timing
- **Re-engagement**: gradually increasing urgency with eventual graceful stop
- **Weak areas**: targeted practice suggestions
- **Score trends**: positive reinforcement or early intervention

---

## 8. Session Lifecycle & Limits

### Session States

```
No session
  │
  ├─ User sends message → Create session
  │   ├─ Pool slot acquired (semaphore)
  │   ├─ Redis lock acquired (SET NX)
  │   ├─ System prompt built (user profile snapshot)
  │   ├─ SDK subprocess started
  │   └─ Session active
  │
  ├─ Active session
  │   ├─ User sends messages → processed by agent
  │   ├─ At 80% turn limit → hook injects "wrap up" hint to agent
  │   ├─ User warned about approaching limits (Telegram message)
  │   │
  │   ├─ Close triggers:
  │   │   ├─ /end command → explicit close
  │   │   ├─ Turn limit reached → auto-close + summary
  │   │   ├─ Cost limit reached → auto-close + summary
  │   │   ├─ Idle timeout (5min free / 10min premium) → auto-close + summary
  │   │   ├─ Setting changed (difficulty/style/timezone/language) → auto-close
  │   │   ├─ Redis lock lost → error close
  │   │   ├─ SDK error → error close
  │   │   └─ Bot shutdown → shutdown close
  │   │
  │   └─ On close:
  │       ├─ SDK subprocess terminated
  │       ├─ Redis lock released
  │       ├─ Pool slot released
  │       ├─ Session record updated in DB
  │       ├─ Session summary sent to user
  │       └─ Post-session pipeline (background):
  │           ├─ Profile integrity validation
  │           ├─ Streak updated
  │           ├─ Difficulty auto-adjusted (if at extremes)
  │           ├─ last_activity enriched with exercise data
  │           ├─ Milestones detected + celebrations sent
  │           └─ Session history appended (rolling last 5)
  │
  └─ Next message → new session created (if within daily limits)
```

### Idle Timeout Behavior
```
Session created
  │
  ├─ ... time passes with no messages ...
  │
  ├─ At 70% of timeout (3.5min free / 7min premium):
  │   └─ User receives idle warning: "Session will close in ~N minutes"
  │       (one-shot: only warned once per session)
  │
  └─ At 100% of timeout:
      └─ Session auto-closed + summary sent
```

### Tier Limits Summary

| Limit | Free | Premium |
|---|---|---|
| Model | claude-haiku-4-5 | claude-sonnet-4-6 |
| Max turns/session | 20 | 30 |
| Max sessions/day | 5 | unlimited |
| Max cost/session | $0.20 | $1.50 |
| Max cost/day | $0.50 | $8.00 |
| Idle timeout | 5 min | 10 min |
| Thinking | disabled | enabled (3000 budget) |
| LLM notifications/day | 2 | 8 |
| Rate limit | 8 msg/min | 20 msg/min |

### What the User Experiences at Limits
- **Turn limit approaching**: warning message ("N turns remaining")
- **Turn limit reached**: session summary → next message creates new session
- **Cost limit**: session summary → next message creates new session
- **Daily session limit**: "You've used 5 sessions today" (free tier only)
- **Daily cost limit**: "Daily budget reached, try again tomorrow"
- **Rate limit**: "Too many messages, please slow down"
- **Pool full**: "Bot is busy, try again in a moment"

### How It Solves the Task
The session lifecycle ensures:
- **Cost control**: prevents runaway spending via hard limits
- **Fair access**: pool semaphores prevent server overload
- **No data loss**: post-session pipeline always runs, even on error
- **Smooth UX**: warnings before limits hit, summaries on close, auto-resume on next message

---

## 9. Account Management

### /help Command (context-aware)
The help message adapts to user state:

| User State | Help Content |
|---|---|
| Not onboarded | "Use /start to begin setup" |
| Onboarded, 0 sessions | "Getting started" guide (how to send first message) |
| Active, returning after 72h+ | "Welcome back" guide (review, refresh, commands) |
| Regular user | Full help with all commands |

### /deleteme Command
```
/deleteme
  │
  └─ Confirmation prompt:
      "This will permanently delete ALL your data..."
      [Yes, delete everything] [Cancel]
            │                      │
            ▼                      └─ Back to settings
      - Active session closed
      - User record deleted (CASCADE to all related tables)
      - Confirmation message sent
      - User can re-register by sending /start again
```

### What Gets Deleted (CASCADE)
- User profile
- All vocabulary (+ review logs)
- All sessions
- All schedules
- All exercise results
- All notifications

### What the User Cannot Do
- Partially delete data (it's all or nothing)
- Export data before deletion
- Undo deletion (irreversible)
- Delete while keeping vocabulary

### How It Solves the Task
GDPR-friendly complete data erasure. Simple, two-tap process with clear warning.

---

## 10. Admin Paths

### Who Is an Admin
1. Users with `is_admin=True` in DB (primary)
2. Users whose Telegram ID is in `ADMIN_TELEGRAM_IDS` env var (bootstrap)

### Admin-Only Commands

#### /debug
Toggles debug overlay after each message:
```
--- Debug Info ---
Tools:       record_exercise_result, add_vocabulary (2)
Msg cost:    $0.001234
Total cost:  $0.012345
Turns:       5 (remaining: 15)
Tier:        free
Model:       claude-haiku-4-5-20251001
Duration:    12.3s
Active sess: 3
```

### What Admins Receive (automatic)
- **Health alerts** (every 60s check): cost spikes, high pool usage, pipeline failures, notification failures, Redis/DB down
- **Stats reports** (every 12h): user counts, session counts, costs, pool utilization

### Gradio Admin Panel (separate process)
5 tabs: Users, Sessions, Costs, Alerts, System — detailed in CLAUDE.md.

---

## 11. What Users Cannot Do

### Explicitly Blocked
| Action | Reason |
|---|---|
| Change their CEFR level directly | Agent-assessed via learning plan completion to ensure holistic evaluation |
| Change their tier | Admin-assigned, no billing system |
| Send non-text content | Bot only processes text (photos, voice, stickers rejected) |
| Have multiple concurrent sessions | Redis lock enforces one-at-a-time |
| Access other users' data | All queries scoped to `user.telegram_id` |
| See system prompt | Agent instructed to refuse |
| Change native language post-onboarding | Must delete account and re-register |
| Create schedules from settings UI | Only via agent conversation or onboarding |
| Set custom quiet hours | Preset options only |
| See cost/usage data | Only visible in /stats (limited) and admin panel |
| Review cards not belonging to them | Ownership check on every review action |

### Silently Ignored
| Action | What Happens |
|---|---|
| Duplicate vocabulary word | Agent deduplicates (case-insensitive) |
| Exceeding array limits | Silently capped (interests ≤ 8, goals ≤ 5, etc.) |
| Rating a card already reviewed | "Already reviewed" message, no double-processing |
| Sending message during rate limit | Rate limit message, event dropped |
| Rapid button taps | Row-level locking prevents duplicate processing |

---

## 12. Edge Cases & Error States

### User Blocks the Bot
- Next notification attempt gets `TelegramForbiddenError`
- User's `is_active` flag set to `False`
- No more notifications sent
- User can unblock and send a message → AuthMiddleware handles them normally

### Bot Restarts During Active Session
- All in-memory sessions lost (SDK subprocesses die)
- Redis session locks expire naturally (TTL-based)
- Next user message creates a fresh session with latest profile state
- Post-session pipeline may not run for interrupted sessions → shows as `pending` pipeline status

### Concurrent Race Conditions
| Scenario | Protection |
|---|---|
| Two messages from same user simultaneously | Session-level `asyncio.Lock` serializes processing |
| Cleanup loop closes session while processing | Re-check after lock acquisition, retry with new session (up to 3 attempts) |
| Two proactive ticks overlap | Distributed Redis lock on tick function |
| Two notifications for same user/type | Redis SET NX dedup + DB atomic daily limit check |
| Setting change during active session | Closes session → next message gets new session with updated prompt |
| User deletes account during session | Session continues until natural close, post-session detects missing user |

### Network/Service Failures
| Failure | Behavior |
|---|---|
| Redis down | Rate limit: fail open (allow). Dedup: fail open (allow). Session lock: fail closed (block). |
| DB error during session creation | User gets "Please try again" message |
| SDK subprocess crash | Error logged, session closed, next message creates fresh session |
| SDK close timeout (5s) | Process killed, resources released |
| Telegram API failure on notification | Dedup slot released for retry on next tick. Exponential backoff for schedules. |
| Post-session pipeline failure | Retry once after 2s. If still fails, logged + session marked `pipeline_failed`. |

### Schedule Failure Handling
```
Schedule trigger fails
  │
  ├─ Failure count < 10:
  │   └─ Exponential backoff: next retry in 2^N minutes (max 24h)
  │
  └─ Failure count ≥ 10:
      ├─ Schedule auto-paused
      └─ User notified: "Schedule X has been paused due to repeated failures"
```

---

## Summary: User Journey Timeline

```
Day 0:
  /start → Onboarding (6 steps) → First message → First session
  Agent: greets, assesses level, teaches first words, records scores

Day 1:
  Notification: "You have 5 cards due!" → /review → FSRS review session
  New message → Agent remembers yesterday, continues from where they left off

Day 3:
  /stats → See progress (level, streak, vocabulary)
  Notification: "Don't lose your 3-day streak!" (evening)

Day 7:
  Weekly progress report (LLM-generated, sent Sunday)
  Agent: notices weak areas from last week, focuses exercises

Day 14:
  /settings → Change difficulty to hard (getting bored)
  Agent: adapts immediately — complex structures, less hints

Day 30:
  🎉 Milestone: 30-day streak celebration!
  🎉 Milestone: 100 vocabulary words!
  Agent: celebrates, suggests new goals

Day 60:
  /settings → Change language (Japanese → French)
  Confirmation: "Delete 200+ words and reset?" → Fresh start

Day 90+:
  Premium tier (admin-assigned) → Better model, more turns, thinking
  /deleteme → Complete data erasure → Can re-register anytime
```
