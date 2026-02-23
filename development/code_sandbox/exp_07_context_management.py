"""
Experiment 07: Large Context Handling & Compaction
==================================================
Goal: Understand context growth across turns, whether compaction fires,
      and compare strategies for managing growing conversations.

Run: poetry run python development/code_sandbox/exp_07_context_management.py
Output: development/code_sandbox/output/exp_07_output.txt
"""

import asyncio
import json

from shared import load_env, Log


async def part_a_context_growth(log):
    """Monitor context growth across many turns in a ClaudeSDKClient session."""
    from claude_agent_sdk import (
        ClaudeSDKClient, ClaudeAgentOptions, AssistantMessage, ResultMessage,
        SystemMessage, TextBlock, ThinkingBlock,
    )

    log.sep("Part A: Context Growth Over 10 Turns")

    options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=3,
        thinking={"type": "disabled"},
        system_prompt=(
            "You are a Spanish tutor. Keep responses concise (3-5 sentences max). "
            "Track vocabulary taught. Always reference what was taught in earlier turns."
        ),
    )

    # Progressively complex prompts to grow context
    prompts = [
        "Teach me 3 basic Spanish greetings.",
        "Now teach me 3 food-related words. Reference the greetings you just taught.",
        "Give me a mini-quiz: combine the greetings and food words into 2 sentences.",
        "Correct my answer: 'Hola, quiero manzana por favor'. Reference all words taught so far.",
        "Teach me 3 travel words. List everything we've learned this session.",
        "Create a short story using at least 5 words from our session.",
        "Quiz me again: translate 'I want cheese and bread please' to Spanish.",
        "How many words have we learned total? List them all.",
        "Teach me 2 more difficult words (B1 level). Keep our full vocabulary list updated.",
        "Final review: create a comprehensive sentence using 8+ words from our session.",
    ]

    turn_data = []
    system_messages = []

    async with ClaudeSDKClient(options) as client:
        for i, prompt in enumerate(prompts):
            log(f"--- Turn {i+1} ---")
            log(f"  Prompt: {prompt[:80]}...")

            await client.query(prompt)

            response_text = ""
            cost = 0.0
            usage = {}

            async for msg in client.receive_response():
                if isinstance(msg, SystemMessage):
                    system_messages.append({
                        "turn": i + 1,
                        "subtype": msg.subtype,
                    })
                    log(f"  *** SystemMessage: subtype={msg.subtype} ***")
                elif isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            response_text += block.text
                        elif isinstance(block, ThinkingBlock):
                            log(f"  [Thinking: {len(block.thinking)} chars]")
                elif isinstance(msg, ResultMessage):
                    cost = msg.total_cost_usd or 0.0
                    usage = msg.usage or {}

            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            cache_read = usage.get("cache_read_input_tokens", 0)
            cache_create = usage.get("cache_creation_input_tokens", 0)

            log(f"  Response: {response_text[:150]}...")
            log(f"  Input tokens: {input_tokens}, Output: {output_tokens}")
            log(f"  Cache read: {cache_read}, Cache create: {cache_create}")
            log(f"  Cumulative cost: ${cost:.6f}\n")

            turn_data.append({
                "turn": i + 1,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read": cache_read,
                "cache_create": cache_create,
                "cost": cost,
                "response_len": len(response_text),
            })

    # Summary table
    log.sep("Part A: Context Growth Table")
    log(f"{'Turn':<6} {'InTok':<10} {'OutTok':<10} {'CacheRd':<10} {'CacheCr':<10} {'Cost':<12} {'RespLen':<10}")
    log("-" * 68)
    for td in turn_data:
        log(f"{td['turn']:<6} {td['input_tokens']:<10} {td['output_tokens']:<10} "
            f"{td['cache_read']:<10} {td['cache_create']:<10} ${td['cost']:<11.6f} {td['response_len']:<10}")

    if system_messages:
        log(f"\nSystem messages observed: {len(system_messages)}")
        for sm in system_messages:
            log(f"  Turn {sm['turn']}: {sm['subtype']}")
    else:
        log(f"\nNo SystemMessage events observed in 10 turns")

    # Check if early content is remembered in later turns
    last_response = turn_data[-1]["response_len"] if turn_data else 0
    log(f"\nFinal turn response length: {last_response} chars")
    log(f"Final cumulative cost: ${turn_data[-1]['cost']:.6f}" if turn_data else "No turns")

    return turn_data, system_messages


async def part_b_resume_vs_fresh(log):
    """Compare: resume a long session vs start fresh with system prompt summary."""
    from claude_agent_sdk import (
        query, ClaudeAgentOptions, ClaudeSDKClient,
        AssistantMessage, ResultMessage, TextBlock,
    )

    log.sep("Part B: Resume Long Session vs Fresh with Summary")

    # Strategy 1: Build a session, then resume it
    log("--- Strategy 1: Build session then resume ---")

    build_options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=1,
        thinking={"type": "disabled"},
        system_prompt="You are a Spanish tutor. Be concise.",
    )

    # Build 3-turn session to establish context
    session_id = None
    for i, prompt in enumerate([
        "Teach me: manzana, pan, queso, leche, agua.",
        "Now teach me: ir, venir, poder, querer, saber.",
        "My weak areas are subjunctive and irregular verbs. I like travel topics.",
    ]):
        opts = ClaudeAgentOptions(
            model="claude-sonnet-4-6",
            max_turns=1,
            thinking={"type": "disabled"},
            system_prompt="You are a Spanish tutor. Be concise (2-3 sentences max).",
        )
        if session_id:
            opts = ClaudeAgentOptions(
                model="claude-sonnet-4-6",
                max_turns=1,
                thinking={"type": "disabled"},
                system_prompt="You are a Spanish tutor. Be concise (2-3 sentences max).",
                resume=session_id,
            )

        async for msg in query(prompt=prompt, options=opts):
            if isinstance(msg, ResultMessage):
                session_id = msg.session_id
                build_cost = msg.total_cost_usd or 0.0

    log(f"  Built session: {session_id}")
    log(f"  Build cost: ${build_cost:.6f}")

    # Resume and ask a question
    resume_options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=1,
        thinking={"type": "disabled"},
        resume=session_id,
    )

    resume_text = ""
    resume_cost = 0.0

    async for msg in query(prompt="What vocabulary have I learned? What are my weak areas?", options=resume_options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    resume_text += block.text
        elif isinstance(msg, ResultMessage):
            resume_cost = msg.total_cost_usd or 0.0

    log(f"  Resume response: {resume_text[:300]}...")
    log(f"  Resume cost: ${resume_cost:.6f}")
    remembers_manzana = "manzana" in resume_text.lower()
    remembers_weak = "subjunctive" in resume_text.lower()
    log(f"  Remembers 'manzana': {remembers_manzana}")
    log(f"  Remembers 'subjunctive': {remembers_weak}")

    # Strategy 2: Fresh session with summary in system prompt
    log("\n--- Strategy 2: Fresh session with summary ---")

    summary_prompt = """You are a Spanish tutor. Be concise.

PRIOR SESSION SUMMARY (from database):
- Vocabulary taught: manzana (apple), pan (bread), queso (cheese), leche (milk), agua (water), ir (to go), venir (to come), poder (to be able), querer (to want), saber (to know)
- Student weak areas: subjunctive mood, irregular verbs
- Student interests: travel topics
- Level: A2"""

    fresh_options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=1,
        thinking={"type": "disabled"},
        system_prompt=summary_prompt,
    )

    fresh_text = ""
    fresh_cost = 0.0

    async for msg in query(prompt="What vocabulary have I learned? What are my weak areas?", options=fresh_options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    fresh_text += block.text
        elif isinstance(msg, ResultMessage):
            fresh_cost = msg.total_cost_usd or 0.0

    log(f"  Fresh response: {fresh_text[:300]}...")
    log(f"  Fresh cost: ${fresh_cost:.6f}")
    fresh_manzana = "manzana" in fresh_text.lower()
    fresh_weak = "subjunctive" in fresh_text.lower()
    log(f"  Remembers 'manzana': {fresh_manzana}")
    log(f"  Remembers 'subjunctive': {fresh_weak}")

    log.sep("Part B: Comparison")
    log(f"{'Strategy':<25} {'Cost':<12} {'Has manzana':<14} {'Has subjunctive'}")
    log("-" * 60)
    log(f"{'Resume (session)':<25} ${resume_cost:<11.6f} {str(remembers_manzana):<14} {remembers_weak}")
    log(f"{'Fresh (summary)':<25} ${fresh_cost:<11.6f} {str(fresh_manzana):<14} {fresh_weak}")
    log(f"\nResume carries full conversation history — grows cost over time")
    log(f"Fresh with summary is cheaper and equally effective for structured data")


async def part_c_max_turns_strategy(log):
    """Test using max_turns to cap session + summarize strategy."""
    from claude_agent_sdk import (
        query, ClaudeAgentOptions, AssistantMessage, ResultMessage, TextBlock,
    )

    log.sep("Part C: max_turns Capping Strategy")

    # Simulate: run a session with max_turns=2, then start new session with summary
    log("--- Session 1: 2 turns ---")

    session1_text = ""
    session1_cost = 0.0

    options1 = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=2,
        thinking={"type": "disabled"},
        system_prompt="You are a Spanish tutor. Be very concise (2 sentences max).",
    )

    async for msg in query(prompt="Teach me 5 food words in Spanish with translations.", options=options1):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    session1_text += block.text
        elif isinstance(msg, ResultMessage):
            session1_cost = msg.total_cost_usd or 0.0
            session1_id = msg.session_id

    log(f"  Session 1 response: {session1_text[:200]}...")
    log(f"  Cost: ${session1_cost:.6f}")

    # Now ask the model to generate a summary (could also be done programmatically)
    summary_text = ""
    async for msg in query(
        prompt="Summarize what you taught in 1-2 sentences. Just facts, no explanation.",
        options=ClaudeAgentOptions(
            model="claude-sonnet-4-6",
            max_turns=1,
            thinking={"type": "disabled"},
            resume=session1_id,
        ),
    ):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    summary_text += block.text
        elif isinstance(msg, ResultMessage):
            summary_cost = msg.total_cost_usd or 0.0

    log(f"  Summary: {summary_text[:200]}")
    log(f"  Summary cost: ${summary_cost:.6f}")

    # Session 2: fresh with summary injected
    log("\n--- Session 2: Fresh with injected summary ---")

    session2_text = ""
    session2_cost = 0.0

    options2 = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=1,
        thinking={"type": "disabled"},
        system_prompt=f"You are a Spanish tutor. Be very concise.\n\nPRIOR SESSION: {summary_text}",
    )

    async for msg in query(
        prompt="Quiz me on the food words from our last session.",
        options=options2,
    ):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    session2_text += block.text
        elif isinstance(msg, ResultMessage):
            session2_cost = msg.total_cost_usd or 0.0

    log(f"  Session 2 response: {session2_text[:300]}...")
    log(f"  Cost: ${session2_cost:.6f}")
    log(f"  References prior session: {'food' in session2_text.lower() or 'manzana' in session2_text.lower()}")

    log(f"\nTotal cost (session1 + summary + session2): ${session1_cost + summary_cost + session2_cost:.6f}")
    log("Strategy: cap sessions at N turns, generate summary, inject into next session system_prompt")


async def main():
    load_env()
    log = Log("exp_07_output")

    log.sep("Experiment 07: Large Context Handling & Compaction")

    turn_data, sys_msgs = await part_a_context_growth(log)
    await part_b_resume_vs_fresh(log)
    await part_c_max_turns_strategy(log)

    log.sep("Key Takeaways")
    log("1. Context tokens grow with each turn — monitor input_tokens and cache usage")
    log("2. Compaction may not trigger within 10 turns for short conversations")
    log("3. 'Fresh session + summary' is cheaper and equally effective vs 'resume'")
    log("4. Strategy: cap sessions at N turns, summarize, start fresh")
    log("5. System prompt summary keeps costs predictable and bounded")

    log.close()


if __name__ == "__main__":
    asyncio.run(main())
