"""
Experiment 03: query() vs ClaudeSDKClient — When to Use Which
==============================================================
Goal: Directly compare the two main APIs to understand their tradeoffs.

Run: poetry run python development/code_sandbox/exp_03_query_vs_client.py
Output: development/code_sandbox/output/exp_03_output.txt
"""

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

from shared import load_env, Log, extract_text, extract_result


async def part_a_query_string(log):
    """Part A1: query() with string prompt (--print mode)."""
    from claude_agent_sdk import query, ClaudeAgentOptions

    log.sep("Part A1: query() with string prompt")

    options = ClaudeAgentOptions(model="claude-sonnet-4-6", max_turns=1)

    start = time.monotonic()
    messages = []
    async for msg in query(
        prompt="Say 'hello from string mode' and tell me what mode you're running in. Be brief (1-2 sentences).",
        options=options,
    ):
        messages.append(msg)
    wall_time = (time.monotonic() - start) * 1000

    text = extract_text(messages)
    result = extract_result(messages)
    log(f"Response: {text[:300]}")
    log(f"Wall time: {wall_time:.0f}ms")
    log(f"Cost: ${result.get('cost', 0):.6f}")
    log(f"Session: {result.get('session_id', 'N/A')}")
    return {"mode": "query_string", "text": text, "result": result, "wall_ms": wall_time, "msg_count": len(messages)}


async def part_a_query_async_iterable(log):
    """Part A2: query() with AsyncIterable prompt (streaming mode)."""
    from claude_agent_sdk import query, ClaudeAgentOptions

    log.sep("Part A2: query() with AsyncIterable prompt")

    options = ClaudeAgentOptions(model="claude-sonnet-4-6", max_turns=1)

    async def prompt_stream() -> AsyncIterator[dict[str, Any]]:
        yield {
            "type": "user",
            "message": {
                "role": "user",
                "content": "Say 'hello from streaming mode' and tell me what mode you're running in. Be brief (1-2 sentences).",
            },
        }

    start = time.monotonic()
    messages = []
    async for msg in query(prompt=prompt_stream(), options=options):
        messages.append(msg)
    wall_time = (time.monotonic() - start) * 1000

    text = extract_text(messages)
    result = extract_result(messages)
    log(f"Response: {text[:300]}")
    log(f"Wall time: {wall_time:.0f}ms")
    log(f"Cost: ${result.get('cost', 0):.6f}")
    log(f"Session: {result.get('session_id', 'N/A')}")
    return {"mode": "query_async", "text": text, "result": result, "wall_ms": wall_time, "msg_count": len(messages)}


async def part_b_client_multi_turn(log):
    """Part B: ClaudeSDKClient multi-turn conversation with context persistence."""
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

    log.sep("Part B: ClaudeSDKClient — multi-turn (3 turns)")

    options = ClaudeAgentOptions(model="claude-sonnet-4-6", max_turns=2)

    start = time.monotonic()
    turn_results = []

    async with ClaudeSDKClient(options) as client:
        turns = [
            "My favorite number is 42. Remember this. Just acknowledge briefly.",
            "What is my favorite number? Just answer the number.",
            "Now multiply my favorite number by 2. Just give the result.",
        ]

        for i, prompt in enumerate(turns, 1):
            log(f"\n--- Turn {i} ---")
            turn_start = time.monotonic()
            await client.query(prompt)

            msgs = []
            async for msg in client.receive_response():
                msgs.append(msg)

            text = extract_text(msgs)
            result = extract_result(msgs)
            turn_time = (time.monotonic() - turn_start) * 1000

            log(f"  Prompt: {prompt}")
            log(f"  Response: {text[:200]}")
            log(f"  Turn time: {turn_time:.0f}ms | Cost: ${result.get('cost', 0):.6f}")
            log(f"  Session: {result.get('session_id', 'N/A')}")
            turn_results.append({"turn": i, "text": text, "result": result, "time_ms": turn_time})

    wall_time = (time.monotonic() - start) * 1000

    log.sep("Context Persistence Check")
    session_ids = [r["result"].get("session_id", "") for r in turn_results]
    log(f"Session IDs: {session_ids}")
    log(f"All same session: {len(set(session_ids)) == 1}")
    log(f"Turn 2 mentions '42': {'42' in turn_results[1]['text']}")
    log(f"Turn 3 mentions '84': {'84' in turn_results[2]['text']}")
    log(f"Total wall time: {wall_time:.0f}ms")

    return {"mode": "client_multi", "turns": turn_results, "wall_ms": wall_time}


async def part_b_client_interrupt(log):
    """Part B2: Test ClaudeSDKClient interrupt."""
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, AssistantMessage, TextBlock

    log.sep("Part B2: ClaudeSDKClient — interrupt test")

    options = ClaudeAgentOptions(model="claude-sonnet-4-6", max_turns=1)

    async with ClaudeSDKClient(options) as client:
        await client.query("Write a very long detailed essay about the history of language learning. Make it at least 500 words.")

        msg_count = 0
        text_so_far = ""
        interrupted = False

        async for msg in client.receive_response():
            msg_count += 1
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text_so_far += block.text

            if len(text_so_far) > 100 and not interrupted:
                log(f"  Interrupting after {len(text_so_far)} chars...")
                try:
                    await client.interrupt()
                    interrupted = True
                    log("  Interrupt sent successfully")
                except Exception as e:
                    log(f"  Interrupt failed: {e}")

        log(f"  Messages received: {msg_count}")
        log(f"  Text length: {len(text_so_far)}")
        log(f"  Was interrupted: {interrupted}")
        log(f"  Text preview: {text_so_far[:200]}...")

    return {"interrupted": interrupted, "text_len": len(text_so_far)}


async def main():
    load_env()
    log = Log("exp_03_output")

    log.sep("Experiment 03: query() vs ClaudeSDKClient")

    r1 = await part_a_query_string(log)
    r2 = await part_a_query_async_iterable(log)
    r3 = await part_b_client_multi_turn(log)
    r4 = await part_b_client_interrupt(log)

    # Comparison table
    log.sep("Comparison Table")
    log(f"{'Mode':<25} {'Wall ms':>10} {'Cost':>12} {'Msgs':>6}")
    log("-" * 55)
    for r in [r1, r2]:
        log(f"{r['mode']:<25} {r['wall_ms']:>10.0f} ${r['result'].get('cost', 0):.6f}    {r['msg_count']:>6}")

    log.sep("When to Use Which")
    log("query(prompt=str):         Simple one-shot tasks, scheduled jobs")
    log("query(prompt=AsyncIter):   Tasks needing custom MCP tools, no interactivity")
    log("ClaudeSDKClient:           Interactive chat, multi-turn, hooks, interrupts")

    log.close()


if __name__ == "__main__":
    asyncio.run(main())
