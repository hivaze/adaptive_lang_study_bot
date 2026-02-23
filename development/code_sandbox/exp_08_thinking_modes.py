"""
Experiment 08: Thinking Modes & Their Impact
=============================================
Goal: Understand thinking config (disabled/adaptive/enabled), effort levels,
      how thinking interacts with tool use and multi-turn conversations.

Run: poetry run python development/code_sandbox/exp_08_thinking_modes.py
Output: development/code_sandbox/output/exp_08_output.txt
"""

import asyncio
import json
import time
from typing import Any

from shared import load_env, Log, extract_text, extract_result, PROJECT_ROOT


async def run_query_with_thinking(log, label: str, thinking_config, prompt: str, extra_opts: dict | None = None):
    """Run a query with a specific thinking config and return metrics."""
    from claude_agent_sdk import (
        query, ClaudeAgentOptions, AssistantMessage, ResultMessage,
        TextBlock, ThinkingBlock,
    )

    opts_kwargs = dict(
        model="claude-sonnet-4-6",
        max_turns=1,
        thinking=thinking_config,
    )
    if extra_opts:
        opts_kwargs.update(extra_opts)

    options = ClaudeAgentOptions(**opts_kwargs)

    thinking_text = ""
    response_text = ""
    thinking_blocks_count = 0

    t0 = time.monotonic()
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ThinkingBlock):
                    thinking_blocks_count += 1
                    thinking_text += block.thinking
                elif isinstance(block, TextBlock):
                    response_text += block.text
        elif isinstance(msg, ResultMessage):
            result = {
                "cost": msg.total_cost_usd or 0.0,
                "duration_ms": msg.duration_ms,
                "num_turns": msg.num_turns,
                "usage": msg.usage,
                "session_id": msg.session_id,
            }
    wall_ms = int((time.monotonic() - t0) * 1000)

    log(f"  [{label}]")
    log(f"  Thinking config: {thinking_config}")
    log(f"  Thinking blocks: {thinking_blocks_count}")
    log(f"  Thinking length: {len(thinking_text)} chars")
    if thinking_text:
        log(f"  Thinking preview: {thinking_text[:200]}...")
    log(f"  Response length: {len(response_text)} chars")
    log(f"  Response preview: {response_text[:300]}...")
    log(f"  Cost: ${result.get('cost', 0):.6f}")
    log(f"  Wall time: {wall_ms}ms")
    usage = result.get("usage", {})
    log(f"  Usage: input={usage.get('input_tokens', '?')}, output={usage.get('output_tokens', '?')}")
    log("")

    return {
        "label": label,
        "thinking_blocks": thinking_blocks_count,
        "thinking_chars": len(thinking_text),
        "response_chars": len(response_text),
        "response_text": response_text,
        "thinking_text": thinking_text,
        "cost": result.get("cost", 0),
        "wall_ms": wall_ms,
        "usage": usage,
    }


async def part_a_thinking_comparison(log):
    """Compare thinking modes: disabled, adaptive, enabled(10K), enabled(30K)."""
    log.sep("Part A: Thinking Modes Comparison")

    prompt = (
        "You are a language tutor. A student at A2 Spanish level struggles with "
        "the subjunctive mood. Design a short 3-step micro-lesson to teach them "
        "when to use subjunctive after 'espero que'. Include one example sentence "
        "per step. Be concise."
    )
    log(f"Prompt: {prompt}\n")

    configs = [
        ("disabled", {"type": "disabled"}),
        ("adaptive", {"type": "adaptive"}),
        ("enabled_10k", {"type": "enabled", "budget_tokens": 10_000}),
        ("enabled_30k", {"type": "enabled", "budget_tokens": 30_000}),
    ]

    results = []
    for label, config in configs:
        r = await run_query_with_thinking(log, label, config, prompt)
        results.append(r)

    # Comparison table
    log.sep("Part A: Comparison Table")
    log(f"{'Mode':<16} {'Think#':<8} {'ThinkCh':<10} {'RespCh':<10} {'Cost':<12} {'Wall ms':<10}")
    log("-" * 66)
    for r in results:
        log(f"{r['label']:<16} {r['thinking_blocks']:<8} {r['thinking_chars']:<10} {r['response_chars']:<10} ${r['cost']:<11.6f} {r['wall_ms']:<10}")

    return results


async def part_b_thinking_with_tools(log):
    """Test thinking interleaved with tool use."""
    from claude_agent_sdk import (
        query, ClaudeAgentOptions, AssistantMessage, ResultMessage,
        TextBlock, ThinkingBlock, ToolUseBlock, UserMessage, ToolResultBlock,
    )

    log.sep("Part B: Thinking with Tool Use")

    options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=3,
        allowed_tools=["Read"],
        permission_mode="bypassPermissions",
        cwd=str(PROJECT_ROOT),
        thinking={"type": "enabled", "budget_tokens": 10_000},
    )

    prompt = (
        "Read the pyproject.toml file and analyze which dependencies are essential "
        "for an AI chatbot project vs which are for monitoring/logging. Categorize them briefly."
    )
    log(f"Prompt: {prompt}\n")

    message_sequence = []  # Track order of block types

    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ThinkingBlock):
                    message_sequence.append("THINKING")
                    log(f"  -> ThinkingBlock ({len(block.thinking)} chars): {block.thinking[:150]}...")
                elif isinstance(block, ToolUseBlock):
                    message_sequence.append("TOOL_USE")
                    log(f"  -> ToolUseBlock: {block.name}({str(block.input)[:80]})")
                elif isinstance(block, TextBlock):
                    message_sequence.append("TEXT")
                    log(f"  -> TextBlock: {block.text[:200]}...")
        elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
            for block in msg.content:
                if isinstance(block, ToolResultBlock):
                    message_sequence.append("TOOL_RESULT")
                    log(f"  -> ToolResultBlock (is_error={block.is_error})")
        elif isinstance(msg, ResultMessage):
            log(f"\n  Cost: ${msg.total_cost_usd or 0:.6f}")
            log(f"  Turns: {msg.num_turns}")
            log(f"  Usage: {msg.usage}")

    log(f"\nBlock sequence: {' -> '.join(message_sequence)}")
    thinking_before_tool = False
    thinking_after_tool = False
    for i, s in enumerate(message_sequence):
        if s == "THINKING" and i + 1 < len(message_sequence) and message_sequence[i + 1] == "TOOL_USE":
            thinking_before_tool = True
        if s == "TOOL_RESULT" and i + 1 < len(message_sequence) and message_sequence[i + 1] == "THINKING":
            thinking_after_tool = True
    log(f"Thinking before tool call: {thinking_before_tool}")
    log(f"Thinking after tool result: {thinking_after_tool}")


async def part_c_thinking_multiturn(log):
    """Test thinking across multi-turn conversation with ClaudeSDKClient."""
    from claude_agent_sdk import (
        ClaudeSDKClient, ClaudeAgentOptions, AssistantMessage, ResultMessage,
        TextBlock, ThinkingBlock,
    )

    log.sep("Part C: Thinking in Multi-Turn (ClaudeSDKClient)")

    options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=3,
        thinking={"type": "enabled", "budget_tokens": 10_000},
        system_prompt=(
            "You are a language tutor teaching Spanish vocabulary. "
            "Keep responses concise (2-3 sentences max)."
        ),
    )

    turns = [
        "Teach me 3 Spanish words related to food. Just list them with translations.",
        "Quiz me on those words — give me the Spanish word and I'll guess the English. Start with the first one.",
        "The answer is 'bread'. Was I right? Give feedback and adjust difficulty.",
    ]

    turn_results = []

    async with ClaudeSDKClient(options) as client:
        for i, turn_prompt in enumerate(turns):
            log(f"--- Turn {i+1} ---")
            log(f"  Prompt: {turn_prompt}")

            await client.query(turn_prompt)

            thinking_count = 0
            thinking_total_chars = 0
            response_text = ""
            cost = 0.0

            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, ThinkingBlock):
                            thinking_count += 1
                            thinking_total_chars += len(block.thinking)
                            log(f"  [Thinking] {len(block.thinking)} chars: {block.thinking[:120]}...")
                        elif isinstance(block, TextBlock):
                            response_text += block.text
                elif isinstance(msg, ResultMessage):
                    cost = msg.total_cost_usd or 0.0

            log(f"  Response: {response_text[:200]}")
            log(f"  Thinking blocks: {thinking_count}, chars: {thinking_total_chars}")
            log(f"  Cost: ${cost:.6f}\n")

            turn_results.append({
                "turn": i + 1,
                "thinking_blocks": thinking_count,
                "thinking_chars": thinking_total_chars,
                "response_chars": len(response_text),
                "cost": cost,
            })

    log("--- Multi-Turn Summary ---")
    total_cost = 0
    for tr in turn_results:
        total_cost = tr["cost"]  # cost is cumulative in SDK
        log(f"  Turn {tr['turn']}: thinking={tr['thinking_blocks']} blocks ({tr['thinking_chars']} chars), cost=${tr['cost']:.6f}")
    log(f"  Final cumulative cost: ${total_cost:.6f}")
    log(f"  Thinking present in all turns: {all(tr['thinking_blocks'] > 0 for tr in turn_results)}")


async def part_d_thinking_for_personalization(log):
    """Compare lesson plan quality with thinking OFF vs ON."""
    from claude_agent_sdk import (
        query, ClaudeAgentOptions, AssistantMessage, ResultMessage,
        TextBlock, ThinkingBlock,
    )

    log.sep("Part D: Thinking for Personalization Reasoning")

    user_profile = {
        "name": "Alex",
        "language": "Spanish",
        "level": "A2",
        "weak_areas": ["subjunctive mood", "irregular verbs"],
        "strong_areas": ["basic vocabulary", "present tense"],
        "interests": ["travel", "food"],
        "learning_style": "visual + examples",
        "streak": 12,
        "last_score": "7/10 on irregular verbs quiz",
        "time_available": "15 minutes",
    }

    system_prompt = f"""You are a personalized language tutor. Here is the student's profile:
{json.dumps(user_profile, indent=2)}

Design a personalized 15-minute lesson plan that:
1. Addresses their weakest area
2. Incorporates their interests
3. Matches their learning style
4. Builds on their strengths
Be specific with exercises and examples."""

    prompt = "Create my personalized lesson plan for today."
    log(f"User profile: {json.dumps(user_profile, indent=2)}\n")

    configs = [
        ("thinking_OFF", {"type": "disabled"}),
        ("thinking_ON_10k", {"type": "enabled", "budget_tokens": 10_000}),
    ]

    results = []
    for label, config in configs:
        log(f"--- {label} ---")

        options = ClaudeAgentOptions(
            model="claude-sonnet-4-6",
            max_turns=1,
            thinking=config,
            system_prompt=system_prompt,
        )

        thinking_text = ""
        response_text = ""
        thinking_count = 0

        t0 = time.monotonic()
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, ThinkingBlock):
                        thinking_count += 1
                        thinking_text += block.thinking
                    elif isinstance(block, TextBlock):
                        response_text += block.text
            elif isinstance(msg, ResultMessage):
                cost = msg.total_cost_usd or 0.0
                usage = msg.usage
        wall_ms = int((time.monotonic() - t0) * 1000)

        log(f"  Thinking blocks: {thinking_count}")
        if thinking_text:
            log(f"  Thinking preview: {thinking_text[:300]}...")
        log(f"  Response length: {len(response_text)} chars")
        log(f"  Response preview: {response_text[:500]}...")
        log(f"  Cost: ${cost:.6f}")
        log(f"  Wall time: {wall_ms}ms")
        log(f"  Usage: {usage}\n")

        # Quality indicators (simple heuristic checks)
        mentions_weak_area = "subjunctive" in response_text.lower()
        mentions_interest = "travel" in response_text.lower() or "food" in response_text.lower()
        mentions_learning_style = "visual" in response_text.lower() or "example" in response_text.lower()
        has_specific_exercises = any(w in response_text.lower() for w in ["exercise", "activity", "practice", "quiz"])
        has_time_structure = any(w in response_text.lower() for w in ["minute", "min", "5 min", "10 min"])

        quality = {
            "mentions_weak_area": mentions_weak_area,
            "mentions_interest": mentions_interest,
            "mentions_learning_style": mentions_learning_style,
            "has_specific_exercises": has_specific_exercises,
            "has_time_structure": has_time_structure,
            "quality_score": sum([mentions_weak_area, mentions_interest, mentions_learning_style,
                                  has_specific_exercises, has_time_structure]),
        }
        log(f"  Quality checks: {json.dumps(quality, indent=4)}")
        log("")

        results.append({
            "label": label,
            "thinking_count": thinking_count,
            "thinking_chars": len(thinking_text),
            "response_chars": len(response_text),
            "cost": cost,
            "wall_ms": wall_ms,
            "quality": quality,
        })

    # Comparison
    log.sep("Part D: Comparison")
    log(f"{'Mode':<20} {'ThinkCh':<10} {'RespCh':<10} {'Quality':<10} {'Cost':<12} {'Wall ms':<10}")
    log("-" * 72)
    for r in results:
        log(f"{r['label']:<20} {r['thinking_chars']:<10} {r['response_chars']:<10} "
            f"{r['quality']['quality_score']}/5{'':<6} ${r['cost']:<11.6f} {r['wall_ms']:<10}")

    off = results[0]
    on = results[1]
    log(f"\nCost difference: ${on['cost'] - off['cost']:.6f} ({((on['cost'] / off['cost']) - 1) * 100 if off['cost'] else 0:.1f}% more)")
    log(f"Quality difference: {on['quality']['quality_score'] - off['quality']['quality_score']} points")
    log(f"Thinking worth it? Cost increase vs quality gain trade-off above.")


async def main():
    load_env()
    log = Log("exp_08_output")

    log.sep("Experiment 08: Thinking Modes & Their Impact")

    part_a_results = await part_a_thinking_comparison(log)
    await part_b_thinking_with_tools(log)
    await part_c_thinking_multiturn(log)
    await part_d_thinking_for_personalization(log)

    log.sep("Key Takeaways")
    log("1. Does Sonnet 4.6 think by default? (Check 'adaptive' vs 'disabled')")
    log("2. Cost of thinking tokens? (Compare across modes)")
    log("3. Does thinking improve tool use decisions? (Part B)")
    log("4. Does thinking improve personalization? (Part D quality scores)")
    log("5. Is thinking preserved across multi-turn? (Part C)")

    log.close()


if __name__ == "__main__":
    asyncio.run(main())
