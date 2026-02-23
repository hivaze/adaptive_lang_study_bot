# Claude Agent SDK Experiment Observations

## Meta
- SDK: claude-agent-sdk 0.1.39
- Model: claude-sonnet-4-6
- Python: 3.12+
- Poetry env: adaptive-lang-study-bot
- Date: 2026-02-20
- Total experiments: 13 (all completed)

---

## Experiment 01: Basic Query & Message Anatomy

### Configuration
```python
ClaudeAgentOptions(model="claude-sonnet-4-6", max_turns=1)
```

### Message Flow
`SystemMessage(init)` -> `AssistantMessage(ThinkingBlock)` -> `AssistantMessage(TextBlock)` -> `ResultMessage`

### Key Findings
- ThinkingBlock appears **by default** with Sonnet 4.6 (adaptive thinking)
- Cost: ~$0.025 per simple query
- Duration: ~3.2s wall time
- ResultMessage contains: `session_id`, `total_cost_usd`, `duration_ms`, `num_turns`, `usage`, `is_error`, `result`
- `usage` dict has: `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`

### Surprises
- ThinkingBlock is always present even without explicit thinking config (adaptive mode)
- `input_tokens` shows very low numbers (3) — most input is cached

### Implications for Bot
- Every query returns a session_id that can be used for resume
- Cost tracking via `total_cost_usd` works reliably
- Cache system reduces token costs significantly

---

## Experiment 02: System Prompt & Persona Control

### Configuration
- A: No system prompt (default Claude Code behavior)
- B: `system_prompt="You are Lingua..."` (full string override)
- C: `SystemPromptPreset(type="preset", preset="claude_code", append="...")` (additive)

### Key Findings
- String `system_prompt` fully overrides Claude Code's built-in system prompt — cheapest at $0.015
- Preset + append keeps Claude Code default + adds instructions — most expensive at $0.045
- All sessions get unique IDs

| Mode | Cost | Behavior |
|------|------|----------|
| No system prompt | $0.025 | Acts as Claude Code assistant |
| String override | $0.015 | Fully custom persona (cheapest) |
| Preset + append | $0.045 | Claude Code + custom rules (3x cost) |

### Implications for Bot
- Use **string `system_prompt`** for the bot — full control, lowest cost
- Preset+append is not worth the 3x cost premium for our use case
- String override does NOT break tool use

---

## Experiment 03: query() vs ClaudeSDKClient

### Key Findings

| Mode | Latency | Cost | Features |
|------|---------|------|----------|
| `query(str)` | ~4.4s | $0.013 | One-shot, `--print` mode, no MCP tools |
| `query(AsyncIterable)` | ~3s | $0.012 | Streaming, supports MCP tools |
| `ClaudeSDKClient` | varies | ~$0.011/turn | Multi-turn, hooks, interrupt, MCP tools |

- `ClaudeSDKClient` multi-turn maintains same session_id, context persists perfectly (42 -> 84)
- Cost grows per turn: $0.011 -> $0.020 -> $0.028 (context accumulation)
- `client.interrupt()` works — stopped generation at 21K chars
- **CRITICAL**: `query()` with string prompt does NOT support MCP tools (uses `--print` mode)

### Implications for Bot
- **Interactive chat**: Use `ClaudeSDKClient`
- **Cron/scheduled tasks**: Use `ClaudeSDKClient` (needs MCP tools)
- **Simple one-shots without tools**: Use `query(str)`

---

## Experiment 04: Built-in Tool Usage & Permission Modes

### Message Flow with Tools
```
AssistantMsg(ThinkingBlock) -> AssistantMsg(ToolUseBlock) -> UserMsg(ToolResultBlock)
-> [repeat tool cycle] -> AssistantMsg(TextBlock) -> ResultMessage
```

### Key Findings
- Agent used **Glob even when NOT in allowed_tools** — `allowed_tools` is not a strict whitelist
- `permission_mode="plan"` does **NOT prevent tool execution** — tools still run
- `max_turns=1` still allowed 2 tool calls (turns ≠ tool calls)
- `bypassPermissions` works fully headless

### Surprises
- **allowed_tools is advisory, not restrictive** — the agent can use tools not in the list
- **plan mode doesn't block tools** — contrary to expectations

### Implications for Bot
- Don't rely on `allowed_tools` for security — use hooks (PreToolUse) for guardrails instead
- `bypassPermissions` is the right mode for headless/cron execution

---

## Experiment 05: Session Resume & Cross-Session Memory

### Key Findings
- Session ID stays the **SAME** across all resume methods (resume, continue_conversation, ClaudeSDKClient)
- Full context restored on resume — remembers name, language, level, weak areas
- Data accumulates across resumes (added "travel vocabulary" in phase 3, remembered in phase 4)
- Resume cost is essentially the same as initial (~$0.013 each)
- `continue_conversation=True` works identically to `resume=session_id`

| Phase | Cost | Context |
|-------|------|---------|
| Establish | $0.013 | Initial data |
| Resume | $0.013 | All data restored |
| Continue | $0.015 | Accumulated data |
| Client resume | $0.013 | Full context |

### Implications for Bot
- Session resume is free (no cost overhead)
- Session IDs are stable — can store in user DB for later resume
- For short conversations, resume is fine. For long ones, prefer fresh session + summary

---

## Experiment 06: Data Persistence Patterns

### Patterns Tested

| Pattern | Cost (3 turns) | Reliability | Notes |
|---------|----------------|-------------|-------|
| A: System Prompt | $0.034 | 3/3 parsed | Cheapest, fragile parsing |
| B: Tools as DB | $0.175 | Agent writes | 5x cost, 4 reads in turn 1 |
| C: Hybrid | $0.134 | 2 writes | No read calls needed |

### Key Findings
- **Pattern A** (system prompt injection): Cheapest. Structured JSON output parsing worked 3/3 times, but is inherently fragile
- **Pattern B** (tools as DB): Most expensive due to read tool calls. Agent made 4 separate read calls in turn 1
- **Pattern C** (hybrid — snapshot + write tools): Best balance. Profile available immediately via system prompt; tools handle writes only

### Implications for Bot
- **Use Pattern C (Hybrid)**: System prompt carries user snapshot, tools handle updates
- Avoid Pattern B for cost-sensitive use cases (4-5x more expensive)
- Pattern A is viable for cron tasks where cost matters most

---

## Experiment 07: Large Context Handling & Compaction

### Key Findings
- Context grows ~400 cache tokens per turn (13.8K -> 17.8K over 10 turns)
- **No compaction triggered** in 10 turns — only `init` SystemMessages observed
- Input tokens stay at 3 (SDK is efficient with caching)
- Cost: $0.157 for 10 turns total (~$0.016/turn)

### Resume vs Fresh Comparison

| Strategy | Cost | Remembers manzana | Remembers subjunctive |
|----------|------|--------------------|-----------------------|
| Resume | $0.020 | Yes | Yes |
| Fresh + summary | $0.016 | Yes | Yes |

- **Fresh session + summary is 20% cheaper** and equally effective
- Summary in system prompt keeps costs predictable and bounded

### Implications for Bot
- **Cap sessions at N turns**, generate summary, inject into fresh session
- Don't rely on auto-compaction for active users — manage context proactively
- Monthly cost for long sessions would grow unbounded without this strategy

---

## Experiment 08: Thinking Modes & Their Impact

### Thinking Config Options
```python
thinking={"type": "disabled"}      # No thinking
thinking={"type": "adaptive"}      # Default, ~32K budget
thinking={"type": "enabled", "budget_tokens": N}  # Explicit budget
```

### Comparison (same prompt)

| Mode | Think blocks | Think chars | Cost | Wall ms |
|------|-------------|-------------|------|---------|
| disabled | 0 | 0 | $0.034 | 12573 |
| adaptive | 1 | 209 | $0.036 | 12092 |
| enabled_10k | 1 | 215 | $0.021 | 12771 |
| enabled_30k | 1 | 154 | $0.020 | 11104 |

### Key Findings
- Sonnet 4.6 thinks by default (adaptive) — brief blocks (150-215 chars)
- **Thinking budget doesn't scale cost linearly** — enabled modes were actually cheaper than disabled
- Thinking happens **BEFORE tool calls** but NOT after tool results: `THINKING -> TOOL_USE -> TOOL_RESULT -> TEXT`
- Thinking **persists across all multi-turn turns** — each turn gets its own block
- Personalization quality: 5/5 both with and without thinking — only 1.7% cost increase

### Implications for Bot
- Use **disabled thinking** for cron/proactive tasks (slightly cheaper, no quality loss for structured tasks)
- Use **adaptive** for interactive sessions (minimal overhead, better reasoning)
- Extended thinking (10K+) not needed for language tutoring tasks

---

## Experiment 09: Custom Tools (SDK MCP Server)

### Setup
```python
@tool("name", "description", {"param": str})
async def my_tool(args: dict) -> dict:
    return {"content": [{"type": "text", "text": "..."}]}

server = create_sdk_mcp_server(name="langbot", version="1.0.0", tools=[...])
options = ClaudeAgentOptions(
    mcp_servers={"langbot": server},
    allowed_tools=["mcp__langbot__tool_name"],
)
```

### Key Findings
- Tool chaining works reliably: `get_user_profile` -> `get_exercise` -> `record_answer`
- Error handling is graceful: `is_error=True` → agent reports "not found" to user
- Mixed built-in + custom MCP tools work together in same session
- Tool naming: `mcp__<server_name>__<tool_name>`

### Cost
- 2 tool calls: $0.055
- 3 tool calls (chained): $0.083
- Error handling: $0.027

### Implications for Bot
- Custom tools are the backbone of the architecture
- Design tools as thin DB wrappers — let the agent decide when to call them
- Always include `is_error` handling in tool responses

---

## Experiment 10: Hooks for Guardrails & Monitoring

### Hook Types Tested

| Hook | Works | Use Case |
|------|-------|----------|
| PreToolUse (Bash) | Partial | Block dangerous commands |
| PostToolUse (all) | Yes | Log tool usage for analytics |
| Stop | Yes | Session-end cost tracking |
| PostToolUse additionalContext | Yes | Inject system messages |
| UserPromptSubmit | Yes | Log/filter user prompts |

### Key Findings
- **PreToolUse blocking**: Hook fires for safe commands. For `rm -rf`, the **model refused on its own** before calling Bash — hook didn't fire. Double safety layer.
- **PostToolUse logging**: Works for all tools. Great for analytics.
- **Stop hook**: Captures session_id and session end events.
- **System message injection** via `additionalContext`: Agent sees and follows injected instructions. "Session complete." appeared in response.
- **UserPromptSubmit**: Captures all prompts across multi-turn.

### Hook Signature
```python
async def my_hook(input_data, tool_use_id, context) -> dict:
    return {"continue_": True}  # or {"decision": "block", "reason": "..."}
```

### Implications for Bot
- Use PostToolUse for **user analytics** (track tool usage patterns)
- Use Stop hook for **cost tracking per session**
- Use UserPromptSubmit for **audit logging**
- PreToolUse blocking is a safety net, not primary defense (model also self-censors)

---

## Experiment 11: Personalization Approaches Comparison

### Results (2 users x 4 approaches)

| Approach | Avg Cost | Avg Quality | Best For |
|----------|----------|-------------|----------|
| A: System Prompt | $0.025 | 4.0/6 | Cron tasks (cheap, good enough) |
| B: Tool-Based | $0.036 | 4.5/6 | First-time users (flexible) |
| C: Session Resume | $0.022 | 2.0/6 | Avoid — unreliable quality |
| D: Hybrid | $0.042 | 4.5/6 | Interactive chat (best quality) |

### Key Findings
- **Approach C (resume-only) scored 0/6 for one user** — session memory doesn't guarantee the model references all profile details
- **Approach A (system prompt)** is the best cost/quality ratio for structured tasks
- **Approach D (hybrid)** ties with B on quality but costs more due to tool calls
- **Approach B** uses more tool calls (profile + reviews) but quality is high

### Implications for Bot
- **Proactive/cron tasks**: Approach A (system prompt injection) — cheapest, reliable for structured output
- **Interactive sessions**: Approach D (hybrid) — system prompt snapshot + tools for live data
- **Never use session resume alone** for personalization — always inject profile data

---

## Experiment 12: Proactive Scheduling Pattern

### Results

| Task | Avg Cost | Tool Calls | Notification |
|------|----------|-----------|--------------|
| Morning review | $0.050 | 3 (profile, reviews, notify) | Sent |
| Evening quiz | $0.047 | 3 | Sent |
| Weekly summary | $0.047 | 3 | Sent |
| Edge case (no reviews) | $0.042 | 3 | Sent (encouraging) |

### Cost Projection

| Scale | Monthly Cost |
|-------|-------------|
| 1 user (3 tasks/day) | $4.14 |
| 100 users | $414 |
| 1000 users | $4,136 |

### Key Findings
- Proactive sessions work **100% reliably** as cron tasks
- Tool chaining (profile -> reviews -> notification) is consistent
- Session_id from proactive session enables **seamless resume** when user responds
- Edge cases (no reviews, new user) handled gracefully
- Session resume after proactive notification works — agent remembers context

### Implications for Bot
- Proactive pattern is production-ready
- Cost is predictable: ~$0.05/task/user
- Store session_id in DB after proactive task for seamless resume when user responds
- Consider reducing tasks to 1-2/day at scale to manage costs

---

## Experiment 13: End-to-End User Lifecycle Simulation

### 5 Sessions Over "3 Days"

| Session | Cost | Tool Calls | Vocab After |
|---------|------|-----------|-------------|
| 1. Onboarding | $0.076 | 5 | 8 |
| 2. First Lesson | $0.032 | 1 | 8 |
| 3. Proactive Review | $0.044 | 2 | 8 |
| 4. Interactive Practice | $0.017 | 0 | 8 |
| 5. Progress Report | $0.054 | 2 | 8 |
| **Total** | **$0.22** | **10** | |

### Key Findings
- **Total lifecycle cost: $0.22** for 5 sessions — very economical
- Onboarding was the most expensive session (profile setup, multiple writes)
- **Tool write reliability issue**: Sessions 2 & 4 didn't call `record_score` or `add_vocabulary` despite instructions
- Agent needs **very explicit prompting** to consistently call write tools
- Profile data accumulated correctly where tools were called
- Proactive and interactive sessions use identical patterns

### Critical Issue Discovered
- **Agent doesn't reliably call write tools** even when instructed in system prompt
- Workaround: Make tool calls mandatory in prompt ("You MUST call record_score"), or validate post-session
- This is the biggest risk for data integrity in the bot

### Implications for Bot
- Architecture is validated: hybrid pattern (system prompt + tools) works across all session types
- Cost is bounded and predictable (~$0.03-0.08 per session)
- **Write tool reliability needs mitigation**: mandatory tool call prompting, post-session validation, or hooks
- No session resume needed for standard sessions — fresh sessions with DB snapshots are sufficient

---

## Experiment 14: Smooth Conversation Continuity

### Goal
Test patterns for making conversations feel seamless: time-aware greetings, progress acknowledgment, continuation from where user stopped, proactive-to-interactive transitions.

### Core Technique: Session Context Builder

All smoothness comes from a **pure Python function** that computes dynamic context before the session starts — no SDK magic, no session resume. The function:
1. Computes time gap since last session → determines greeting style
2. Loads last activity (topic, score, incomplete exercises) → enables continuation
3. Checks milestones (streak multiples of 10, vocabulary hundreds) → celebrations
4. Injects all of this into the system prompt as structured sections

```
gap < 1h   → "continuation"    (no greeting needed, just keep going)
gap 1-4h   → "short_break"     (brief acknowledgment)
gap 4-10h  → "normal_return"   (quick hello + reference last session)
gap 10-24h → "long_break"      (warm greeting + streak + last session summary)
gap 1-3d   → "day_plus_break"  (enthusiastic welcome, easy warm-up)
gap 3d+    → "long_absence"    (very warm, no guilt, suggest review)
```

### Test A: Time-Aware Greetings

| Gap | Style | Quality | Cost |
|-----|-------|---------|------|
| 0.3h | continuation | 4/5 | $0.027 |
| 2h | short_break | 5/5 | $0.016 |
| 6h | normal_return | 5/5 | $0.016 |
| 14h | long_break | 5/5 | $0.020 |
| 36h | day_plus_break | 5/5 | $0.019 |
| 96h | long_absence | 5/5 | $0.020 |

- 5/5 quality on all gaps except 0.3h (4/5 — missed name but appropriate for continuation)
- Greeting warmth scales correctly with time gap
- Bot references specific words (pudo, quiso, vino) and scores (7/10) in every case
- At 96h gap: "it's been about four days, but who's counting?" — no guilt, warm tone

### Test B: Continuation from Last Session

Tested with an **incomplete exercise** (5 of 8 questions done):

| Scenario | Quality | Key Behavior |
|----------|---------|-------------|
| "Can we continue where I stopped?" | 5/5 | Bot immediately presents Question 6 of 8 with the exact exercise |
| "Hey!" (generic greeting) | 5/5 | Bot proactively offers: "Want to pick up right where you left off?" |
| "I want cooking vocabulary" | 4/5 | Bot acknowledges incomplete task, smoothly transitions to new topic |

- Storing `last_activity.status = "incomplete"` plus `last_exercise` text in the profile is enough for perfect continuation
- The bot naturally offers to resume OR switch, without the user needing to ask

### Test C: Progress & Milestone Acknowledgment

| Scenario | Quality | Celebration |
|----------|---------|-------------|
| 20-day streak | 5/5 | "🔥 20-DAY STREAK — ¡Increíble!" |
| 400 vocabulary | 5/5 | "you've hit 400 words of vocabulary!" |
| Score jump 4→9 | 5/5 | "huge jump from where you were" |

- Milestones detected in pure Python, injected as `celebrations` field in session context
- Bot naturally weaves celebrations into the greeting without being excessive

### Test D: Proactive → Interactive Transition

1. **Cron sends notification** (via tools: get_reviews → send_notification) — cost: $0.061
2. **User responds** in a new session with the notification text stored in system prompt context
3. Bot **doesn't repeat the notification**, jumps straight into the review activity

- Key technique: store the notification text in DB, inject into next session's system prompt as `CONTEXT: USER IS RESPONDING TO A NOTIFICATION`
- No session resume needed — just pass the notification content forward

### Test E: Multi-Turn Mid-Session Continuity

3-turn conversation (teach → quiz → answer) with `ClaudeSDKClient`:
- Turn 1: Taught 3 restaurant words (la cuenta, el mesero, la propina)
- Turn 2: "What was the second word?" → Correctly identified el mesero, created quiz
- Turn 3: User gave wrong answer → Bot correctly corrected

ClaudeSDKClient maintains perfect within-session context across turns. Cost: $0.048 cumulative for 3 turns.

### Key Findings

1. **All smoothness is achieved via system prompt engineering** — no session resume, no special SDK features
2. **Time gap computation is trivial** (Python datetime math) but has massive UX impact
3. **Storing `last_activity` as a structured object** (topic, status, score, last_exercise, words_practiced, session_summary) is the single most important field for continuity
4. **Proactive → interactive transitions** don't need session resume — store notification text in DB, inject as context
5. **Milestones are pure Python** — detect in code, inject as `celebrations` list, bot handles the rest
6. **Cost is unchanged** — the richer system prompt adds ~200 tokens but doesn't noticeably affect cost ($0.016-0.027)

### Data Model for Smooth Conversations

```python
last_activity = {
    "type": "exercise",           # exercise | conversation | review | quiz
    "topic": "irregular verbs",
    "status": "incomplete",       # completed | incomplete | abandoned
    "score": 7,                   # or None if incomplete
    "last_exercise": "Ella ___ (poder) hacerlo.",  # exact exercise text
    "words_practiced": ["pudo", "quiso", "vino"],
    "session_summary": "Started irregular preterite verbs, 5/8 done...",
}
```

This single object, written at session end and loaded into the next session's system prompt, eliminates the need for session resume entirely.

### Implications for Bot
- Implement `compute_session_context()` as a pre-session step — runs in pure Python, zero LLM cost
- Store `last_activity` and `milestones` in user DB, update at session end
- Store proactive notification text in DB for seamless transition when user responds
- No session resume needed for any conversation continuity pattern
- The system prompt is the single source of conversational smoothness

---

## Overall Architecture Recommendations

### Recommended Stack
```
User Input (Telegram)
  -> Session Manager (Python)
    -> Load user profile from DB
    -> Build system_prompt with profile snapshot
    -> ClaudeSDKClient with MCP tools
    -> Tools: get_profile, update_profile, add_vocab, record_score, send_notification
    -> Hooks: PostToolUse (analytics), Stop (cost tracking), UserPromptSubmit (audit)
    -> Store session_id in DB for resume
```

### Key Decisions
1. **SDK Choice**: `claude-agent-sdk` (wraps CLI, but is the official recommended SDK)
2. **API Mode**: `ClaudeSDKClient` for all use cases (multi-turn, tools, hooks)
3. **Personalization**: Hybrid pattern — system prompt snapshot + tools for writes
4. **Context Management**: Cap sessions, summarize, start fresh (no auto-compaction reliance)
5. **Thinking**: Disabled for cron tasks, adaptive for interactive
6. **System Prompt**: Full string override (cheapest, full control)
7. **Permission Mode**: `bypassPermissions` for headless execution
8. **Hooks**: PostToolUse for analytics, Stop for cost tracking

### Cost Model
| Use Case | Cost/Invocation | Monthly (100 users, 1x/day) |
|----------|-----------------|----------------------------|
| Simple query | $0.015 | $45 |
| Interactive session | $0.04-0.08 | $120-240 |
| Proactive notification | $0.05 | $150 |
| 3 tasks/day/user | $0.14 | $420 |

### Risks to Mitigate
1. **Write tool reliability**: Agent may skip write calls. Use mandatory prompting + post-session validation
2. **allowed_tools is advisory**: Don't rely on it for security. Use hooks.
3. **Context growth**: Cap sessions at N turns. Don't rely on auto-compaction.
4. **SDK is CLI wrapper**: Spawns subprocess. Resource usage per session. Consider connection pooling.
5. **Cost at scale**: $4K/month for 1000 users with 3 daily tasks. Optimize by reducing proactive frequency.
