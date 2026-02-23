"""
Experiment 02: System Prompt & Persona Control
===============================================
Goal: Test system_prompt vs append_system_prompt for shaping bot persona.

Run: poetry run python development/code_sandbox/exp_02_system_prompt.py
Output: development/code_sandbox/output/exp_02_output.txt
"""

import asyncio
from shared import load_env, Log, extract_text, extract_result


async def run_query(log, label: str, options, prompt: str):
    """Run a single query and collect results."""
    from claude_agent_sdk import query

    log.sep(f"Query {label}")

    sp = options.system_prompt
    if isinstance(sp, str) and len(sp) > 80:
        sp = sp[:80] + "..."
    log(f"  system_prompt: {sp}")
    log(f"  prompt: {prompt}")
    log()

    messages = []
    async for msg in query(prompt=prompt, options=options):
        messages.append(msg)

    text = extract_text(messages)
    result = extract_result(messages)

    log(f"Response:\n{text}\n")
    log(f"Session ID: {result.get('session_id', 'N/A')}")
    log(f"Cost: ${result.get('cost', 0):.6f}")
    log(f"Duration: {result.get('duration_ms', 0)}ms")

    return {"label": label, "response": text, **result}


async def main():
    load_env()
    log = Log("exp_02_output")

    from claude_agent_sdk import ClaudeAgentOptions

    log.sep("Experiment 02: System Prompt & Persona Control")

    prompt = "What is your name and role? Answer in 2-3 sentences."

    # Query A: No system prompt (default Claude Code behavior)
    options_a = ClaudeAgentOptions(model="claude-sonnet-4-6", max_turns=1)
    result_a = await run_query(log, "A: No system prompt", options_a, prompt)

    # Query B: Full system_prompt override (string)
    options_b = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=1,
        system_prompt=(
            "You are Lingua, a friendly language tutor specializing in German. "
            "You always greet students warmly and respond with a German sentence first, "
            "followed by an English translation. You are enthusiastic about teaching."
        ),
    )
    result_b = await run_query(log, "B: system_prompt (full override)", options_b, prompt)

    # Query C: SystemPromptPreset with append (additive to Claude Code default)
    options_c = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=1,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": (
                "Always end your responses with a motivational quote about learning. "
                'Format it as: [Quote: "..."]'
            ),
        },
    )
    result_c = await run_query(log, "C: preset + append (additive)", options_c, prompt)

    # Comparison
    log.sep("Comparison Summary")
    results = [result_a, result_b, result_c]
    for r in results:
        log(f"\n--- {r['label']} ---")
        log(f"  Session ID: {r.get('session_id', 'N/A')}")
        log(f"  Cost: ${r.get('cost', 0):.6f}")
        log(f"  Duration: {r.get('duration_ms', 0)}ms")
        preview = r["response"][:200].replace("\n", " ")
        log(f"  Response preview: {preview}")

    log.sep("Analysis")
    session_ids = [r.get("session_id", "") for r in results]
    log(f"All session IDs unique: {len(set(session_ids)) == len(session_ids)}")
    log(f"Query B mentions 'Lingua': {'Lingua' in result_b['response']}")
    log(f"Query C has [Quote:]: {'Quote' in result_c['response']}")
    log(f"Cost comparison: A=${result_a.get('cost',0):.6f}  B=${result_b.get('cost',0):.6f}  C=${result_c.get('cost',0):.6f}")

    log.close()


if __name__ == "__main__":
    asyncio.run(main())
