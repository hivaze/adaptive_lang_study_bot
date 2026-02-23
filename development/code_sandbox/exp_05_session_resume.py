"""
Experiment 05: Session Resume & Cross-Session Memory
====================================================
Goal: Deep dive into session persistence — critical for per-user conversation continuity.

Run: poetry run python development/code_sandbox/exp_05_session_resume.py
Output: development/code_sandbox/output/exp_05_output.txt
"""

import asyncio
from shared import load_env, Log, extract_text, extract_result


async def main():
    load_env()
    log = Log("exp_05_output")

    from claude_agent_sdk import query, ClaudeAgentOptions, ClaudeSDKClient

    log.sep("Experiment 05: Session Resume & Cross-Session Memory")

    base_options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=1,
        system_prompt=(
            "You are a language tutor. Remember user details precisely. "
            "When asked about user info, answer directly and concisely."
        ),
    )

    # ─── Phase 1: Establish session ────────────────────────────
    log.sep("Phase 1: Establish session")

    messages = []
    async for msg in query(
        prompt=(
            "My name is Alex. I'm learning Spanish at A2 level. "
            "My weak areas are subjunctive mood and irregular verbs. "
            "Just confirm you understood."
        ),
        options=base_options,
    ):
        messages.append(msg)

    text = extract_text(messages)
    result = extract_result(messages)
    session_id = result.get("session_id", "")

    log(f"Response: {text[:300]}")
    log(f"Session ID: {session_id}")
    log(f"Cost: ${result.get('cost', 0):.6f}")
    log(f"Duration: {result.get('duration_ms', 0)}ms")

    # ─── Phase 2: Resume by session_id ─────────────────────────
    log.sep("Phase 2: Resume by session_id")

    resume_options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=1,
        resume=session_id,
    )

    messages2 = []
    async for msg in query(
        prompt="What is my name, what language am I learning, what level, and what are my weak areas?",
        options=resume_options,
    ):
        messages2.append(msg)

    text2 = extract_text(messages2)
    result2 = extract_result(messages2)

    log(f"Response: {text2[:400]}")
    log(f"Session ID: {result2.get('session_id', '')}")
    log(f"Same session as Phase 1: {result2.get('session_id', '') == session_id}")
    log(f"Cost: ${result2.get('cost', 0):.6f}")
    log(f"Remembers 'Alex': {'Alex' in text2}")
    log(f"Remembers 'Spanish': {'Spanish' in text2 or 'spanish' in text2}")
    log(f"Remembers 'A2': {'A2' in text2}")
    log(f"Remembers 'subjunctive': {'subjunctive' in text2.lower()}")

    # ─── Phase 3: continue_conversation=True ───────────────────
    log.sep("Phase 3: continue_conversation=True")

    continue_options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=1,
        continue_conversation=True,
    )

    messages3 = []
    async for msg in query(
        prompt="Add to my profile that I'm particularly interested in travel vocabulary. Confirm briefly.",
        options=continue_options,
    ):
        messages3.append(msg)

    text3 = extract_text(messages3)
    result3 = extract_result(messages3)

    log(f"Response: {text3[:400]}")
    log(f"Session ID: {result3.get('session_id', '')}")
    log(f"Continues from Phase 2: {result3.get('session_id', '') == result2.get('session_id', '')}")
    log(f"Cost: ${result3.get('cost', 0):.6f}")

    # ─── Phase 4: Resume with ClaudeSDKClient ──────────────────
    log.sep("Phase 4: Resume with ClaudeSDKClient")

    client_options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=1,
        resume=session_id,
    )

    async with ClaudeSDKClient(client_options) as client:
        # Turn 1 in resumed session
        await client.query("Remind me of everything you know about me.")
        msgs_t1 = []
        async for msg in client.receive_response():
            msgs_t1.append(msg)

        text_t1 = extract_text(msgs_t1)
        result_t1 = extract_result(msgs_t1)

        log(f"Turn 1 Response: {text_t1[:400]}")
        log(f"Session ID: {result_t1.get('session_id', '')}")
        log(f"Same as original: {result_t1.get('session_id', '') == session_id}")
        log(f"Cost: ${result_t1.get('cost', 0):.6f}")

        # Turn 2 in same resumed session
        await client.query("What vocabulary topic am I interested in?")
        msgs_t2 = []
        async for msg in client.receive_response():
            msgs_t2.append(msg)

        text_t2 = extract_text(msgs_t2)
        result_t2 = extract_result(msgs_t2)

        log(f"Turn 2 Response: {text_t2[:300]}")
        log(f"Session ID: {result_t2.get('session_id', '')}")
        log(f"Cost: ${result_t2.get('cost', 0):.6f}")
        log(f"Mentions 'travel': {'travel' in text_t2.lower()}")

    # ─── Phase 5: Cost analysis ────────────────────────────────
    log.sep("Phase 5: Cost Analysis")

    costs = {
        "Phase 1 (establish)": result.get("cost", 0),
        "Phase 2 (resume)": result2.get("cost", 0),
        "Phase 3 (continue)": result3.get("cost", 0),
        "Phase 4 Turn 1 (client resume)": result_t1.get("cost", 0),
        "Phase 4 Turn 2 (client follow-up)": result_t2.get("cost", 0),
    }

    total = 0
    for label, cost in costs.items():
        log(f"  {label}: ${cost:.6f}")
        total += cost
    log(f"  TOTAL: ${total:.6f}")

    log()
    log("Key observations:")
    log(f"  Resume cost overhead: ${costs['Phase 2 (resume)'] - costs['Phase 1 (establish)']:.6f} more than initial")
    log(f"  Continue vs Resume: continue=${costs['Phase 3 (continue)']:.6f}, resume=${costs['Phase 2 (resume)']:.6f}")

    # Session ID tracking
    log.sep("Session ID Tracking")
    log(f"Phase 1: {session_id}")
    log(f"Phase 2: {result2.get('session_id', '')}")
    log(f"Phase 3: {result3.get('session_id', '')}")
    log(f"Phase 4 T1: {result_t1.get('session_id', '')}")
    log(f"Phase 4 T2: {result_t2.get('session_id', '')}")

    log.close()


if __name__ == "__main__":
    asyncio.run(main())
