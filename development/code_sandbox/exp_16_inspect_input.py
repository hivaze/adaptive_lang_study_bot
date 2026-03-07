"""
Experiment 16: Inspect Full Input Sent to Model
================================================
Goal: Understand every component that contributes to input tokens:
  - System prompt
  - Tool definitions (MCP)
  - User message
  - Hook context injections
  - Token usage breakdown from ResultMessage

Run: poetry run python development/code_sandbox/exp_16_inspect_input.py
Output: development/code_sandbox/output/exp_16_output.txt
"""

import asyncio
import json

from shared import Log, extract_result, extract_text, load_env


async def test_1_usage_breakdown(log: Log):
    """Plain query — inspect ResultMessage.usage for token breakdown."""
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    log.sep("Test 1: ResultMessage.usage breakdown (no tools, no hooks)")

    system_prompt = "You are a helpful assistant. Reply in one sentence."
    prompt = "What is 2+2?"

    log(f"System prompt ({len(system_prompt)} chars): {system_prompt}")
    log(f"User prompt: {prompt}")
    log()

    options = ClaudeAgentOptions(
        model="claude-haiku-4-5-20251001",
        max_turns=1,
        thinking={"type": "disabled"},
        system_prompt=system_prompt,
        permission_mode="bypassPermissions",
    )

    messages = []
    async for msg in query(prompt=prompt, options=options):
        messages.append(msg)

    text = extract_text(messages)
    result = extract_result(messages)

    log(f"Response: {text}")
    log(f"Cost: ${result.get('cost', 0):.6f}")
    log(f"Usage: {json.dumps(result.get('usage'), indent=2)}")
    log(f"Full ResultMessage fields: {json.dumps({k: v for k, v in result.items() if k != 'usage'}, indent=2, default=str)}")


async def test_2_with_tools(log: Log):
    """Query with MCP tools via ClaudeSDKClient — see how tool definitions affect input tokens."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        TextBlock,
        ToolUseBlock,
        create_sdk_mcp_server,
        tool,
    )

    log.sep("Test 2: With MCP tools — tool definition token cost")

    @tool(
        "get_weather",
        "Get current weather for a city. Returns temperature and conditions.",
        {"type": "object", "properties": {"city": {"type": "string", "description": "City name"}}, "required": ["city"]},
    )
    async def get_weather(args: dict) -> dict:
        city = args.get("city", "unknown")
        return {"content": [{"type": "text", "text": json.dumps({"city": city, "temp": 22, "conditions": "sunny"})}]}

    @tool(
        "translate_word",
        "Translate a single word between languages. Provide source_lang, target_lang, and word.",
        {"type": "object", "properties": {"word": {"type": "string"}, "source_lang": {"type": "string"}, "target_lang": {"type": "string"}}, "required": ["word", "source_lang", "target_lang"]},
    )
    async def translate_word(args: dict) -> dict:
        word = args.get("word", "?")
        return {"content": [{"type": "text", "text": json.dumps({"word": word, "translation": f"[mock: {word}]"})}]}

    server = create_sdk_mcp_server(name="testtools", version="1.0.0", tools=[get_weather, translate_word])
    allowed = ["mcp__testtools__get_weather", "mcp__testtools__translate_word"]

    system_prompt = "You are a helpful assistant. Reply in one sentence. Use tools if relevant."

    async def run_with_tools(label: str, prompt: str, max_turns: int):
        log(f"\n--- {label} ---")
        options = ClaudeAgentOptions(
            model="claude-haiku-4-5-20251001",
            max_turns=max_turns,
            thinking={"type": "disabled"},
            system_prompt=system_prompt,
            permission_mode="bypassPermissions",
            mcp_servers={"testtools": server},
            allowed_tools=allowed,
        )

        client = ClaudeSDKClient(options)
        messages = []
        async with client:
            await client.query(prompt)
            async for msg in client.receive_response():
                messages.append(msg)

        text = extract_text(messages)
        result = extract_result(messages)
        tools_used = [
            block.name
            for msg in messages
            if isinstance(msg, AssistantMessage)
            for block in msg.content
            if isinstance(block, ToolUseBlock)
        ]
        log(f"Response: {text[:200]}")
        if tools_used:
            log(f"Tools used: {tools_used}")
        log(f"Usage: {json.dumps(result.get('usage'), indent=2)}")
        return result

    # A: prompt that WON'T trigger tool use
    result_a = await run_with_tools("A: No tool use (but tools defined)", "What is 2+2?", 1)

    # B: prompt that WILL trigger tool use
    result_b = await run_with_tools("B: With tool use", "What's the weather in Paris?", 3)

    log("\n--- Comparison ---")
    usage_a = result_a.get("usage") or {}
    usage_b = result_b.get("usage") or {}
    total_input_a = (usage_a.get("input_tokens", 0) + usage_a.get("cache_creation_input_tokens", 0) + usage_a.get("cache_read_input_tokens", 0))
    total_input_b = (usage_b.get("input_tokens", 0) + usage_b.get("cache_creation_input_tokens", 0) + usage_b.get("cache_read_input_tokens", 0))
    log(f"No tool call  - total_input: {total_input_a}, output: {usage_a.get('output_tokens')}")
    log(f"With tool call - total_input: {total_input_b}, output: {usage_b.get('output_tokens')}")


async def test_3_stderr_debug(log: Log):
    """Use stderr callback to capture CLI debug output."""
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, AssistantMessage, ResultMessage, TextBlock

    log.sep("Test 3: stderr callback — raw CLI debug lines")

    stderr_lines: list[str] = []

    def capture_stderr(line: str):
        stderr_lines.append(line)

    system_prompt = "You are a math tutor. Always show your work step by step."

    options = ClaudeAgentOptions(
        model="claude-haiku-4-5-20251001",
        max_turns=1,
        thinking={"type": "disabled"},
        system_prompt=system_prompt,
        permission_mode="bypassPermissions",
        stderr=capture_stderr,
    )

    client = ClaudeSDKClient(options)
    async with client:
        await client.query("What is 7 * 8?")
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        log(f"Response: {block.text[:200]}")
            elif isinstance(msg, ResultMessage):
                log(f"Usage: {json.dumps(msg.usage, indent=2)}")
                log(f"Session ID: {msg.session_id}")

    log(f"\nCaptured {len(stderr_lines)} stderr lines:")
    for i, line in enumerate(stderr_lines[:50]):
        log(f"  [{i}] {line[:300]}")
    if len(stderr_lines) > 50:
        log(f"  ... ({len(stderr_lines) - 50} more lines)")


async def test_4_large_system_prompt(log: Log):
    """Compare token usage with small vs large system prompt."""
    from claude_agent_sdk import ClaudeAgentOptions, query

    log.sep("Test 4: System prompt size → input token impact")

    small_prompt = "You are a helpful assistant."
    large_prompt = (
        "You are Lingua, an adaptive AI language tutor.\n\n"
        "## RULES\n"
        + "\n".join(f"- Rule {i}: Always follow best teaching practices for item {i}." for i in range(1, 30))
        + "\n\n## STUDENT PROFILE\n"
        "Name: Test User\n"
        "Native language: English\n"
        "Target language: Spanish\n"
        "Level: B1 Intermediate\n"
        "Interests: cooking, travel, music\n"
        "Goals: conversational fluency, travel preparation\n"
        "Weak areas: subjunctive mood, ser vs estar\n"
        "\n## TEACHING APPROACH\n"
        "Use immersive conversation with corrections inline.\n"
        "Provide exercises after every 3-4 exchanges.\n"
        "Focus on the student's weak areas.\n"
        "Always provide encouragement.\n"
        + "\n".join(f"Guideline {i}: Additional teaching guideline number {i} for comprehensive coverage." for i in range(1, 20))
    )

    user_prompt = "Say hello"

    for label, sp in [("Small", small_prompt), ("Large", large_prompt)]:
        log(f"\n--- {label} system prompt ({len(sp)} chars) ---")

        options = ClaudeAgentOptions(
            model="claude-haiku-4-5-20251001",
            max_turns=1,
            thinking={"type": "disabled"},
            system_prompt=sp,
            permission_mode="bypassPermissions",
        )

        messages = []
        async for msg in query(prompt=user_prompt, options=options):
            messages.append(msg)

        result = extract_result(messages)
        usage = result.get("usage") or {}
        log(f"Response: {extract_text(messages)[:150]}")
        log(f"Input tokens: {usage.get('input_tokens')}")
        log(f"Output tokens: {usage.get('output_tokens')}")
        log(f"Cache creation: {usage.get('cache_creation_input_tokens')}")
        log(f"Cache read: {usage.get('cache_read_input_tokens')}")
        log(f"Cost: ${result.get('cost', 0):.6f}")

    log(f"\nLarge prompt is {len(large_prompt)} chars vs {len(small_prompt)} chars")


async def test_5_session_transcript(log: Log):
    """Find and read the session transcript file after a query."""
    import glob
    from pathlib import Path

    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    log.sep("Test 5: Session transcript file — full conversation record")

    options = ClaudeAgentOptions(
        model="claude-haiku-4-5-20251001",
        max_turns=1,
        thinking={"type": "disabled"},
        system_prompt="You are a pirate. Respond in pirate speak.",
        permission_mode="bypassPermissions",
    )

    session_id = None
    async for msg in query(prompt="Hello there!", options=options):
        if isinstance(msg, ResultMessage):
            session_id = msg.session_id
            log(f"Session ID: {session_id}")
            log(f"Usage: {json.dumps(msg.usage, indent=2)}")

    if not session_id:
        log("No session ID found!")
        return

    # Search for transcript file
    claude_dir = Path.home() / ".claude"
    log(f"\nSearching for transcript in {claude_dir}...")

    patterns = [
        str(claude_dir / "projects" / "**" / f"{session_id}*"),
        str(claude_dir / "**" / "sessions" / f"{session_id}*"),
        str(claude_dir / "**" / f"*{session_id[:8]}*"),
    ]

    found_files = []
    for pattern in patterns:
        found_files.extend(glob.glob(pattern, recursive=True))

    if found_files:
        log(f"Found {len(found_files)} file(s):")
        for f in found_files[:5]:
            log(f"  {f}")
            try:
                content = Path(f).read_text()
                # Show first 3000 chars of transcript
                log(f"  Content ({len(content)} chars total):")
                for line in content[:3000].split("\n"):
                    log(f"    {line[:200]}")
                if len(content) > 3000:
                    log(f"    ... ({len(content) - 3000} more chars)")
            except Exception as e:
                log(f"  Error reading: {e}")
    else:
        log("No transcript files found. Trying broader search...")
        # List what's in the sessions dirs
        session_dirs = list(claude_dir.glob("**/sessions"))
        for sd in session_dirs[:3]:
            log(f"  Dir: {sd}")
            files = sorted(sd.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
            for f in files:
                log(f"    {f.name} ({f.stat().st_size} bytes)")


async def main():
    load_env()
    log = Log("exp_16_output")

    log.sep("Experiment 16: Inspect Full Input Sent to Model")
    log("This experiment explores every way to see what the SDK sends to the model.\n")

    await test_1_usage_breakdown(log)
    await test_2_with_tools(log)
    await test_3_stderr_debug(log)
    await test_4_large_system_prompt(log)
    await test_5_session_transcript(log)

    log.sep("Summary")
    log("Key findings:")
    log("1. ResultMessage.usage — token counts (input, output, cache)")
    log("2. MCP tool definitions add to input tokens even when not called")
    log("3. stderr callback captures CLI-level debug info")
    log("4. System prompt size directly maps to input token count")
    log("5. Session transcript files contain full conversation history")

    log.close()


if __name__ == "__main__":
    asyncio.run(main())
