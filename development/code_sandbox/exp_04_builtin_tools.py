"""
Experiment 04: Built-in Tool Usage & Permission Modes
=====================================================
Goal: Understand allowed_tools, tool message flow, and permission modes.

Run: poetry run python development/code_sandbox/exp_04_builtin_tools.py
Output: development/code_sandbox/output/exp_04_output.txt
"""

import asyncio
from shared import load_env, Log, PROJECT_ROOT


async def test_tool_usage(log):
    """Test built-in tool usage with Read and Bash."""
    from claude_agent_sdk import (
        query, ClaudeAgentOptions, AssistantMessage, ResultMessage,
        SystemMessage, UserMessage, TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock,
    )

    log.sep("Part A: Tool Usage with bypassPermissions")

    options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=3,
        allowed_tools=["Read", "Bash"],
        permission_mode="bypassPermissions",
        cwd=str(PROJECT_ROOT),
    )

    log(f"Options: allowed_tools={options.allowed_tools}, permission_mode={options.permission_mode}")
    log(f"  cwd={options.cwd}, max_turns={options.max_turns}")
    log(f"Prompt: Read pyproject.toml and list the project dependencies.\n")

    message_flow = []
    tool_calls = []

    async for msg in query(
        prompt="Read the pyproject.toml file and list all the project dependencies with their version constraints. Be concise.",
        options=options,
    ):
        msg_type = type(msg).__name__
        idx = len(message_flow)

        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    tool_calls.append({"name": block.name, "id": block.id, "input": block.input})
                    log(f"  [{idx}] AssistantMessage -> ToolUseBlock: {block.name}")
                    log(f"      id: {block.id}")
                    log(f"      input: {str(block.input)[:200]}")
                elif isinstance(block, TextBlock):
                    log(f"  [{idx}] AssistantMessage -> TextBlock")
                    log(f"      text: {block.text[:300]}{'...' if len(block.text) > 300 else ''}")
                elif isinstance(block, ThinkingBlock):
                    log(f"  [{idx}] AssistantMessage -> ThinkingBlock")
                    log(f"      thinking: {block.thinking[:150]}...")

        elif isinstance(msg, UserMessage):
            if isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, ToolResultBlock):
                        content_preview = str(block.content)[:150] if block.content else "None"
                        log(f"  [{idx}] UserMessage -> ToolResultBlock")
                        log(f"      tool_use_id: {block.tool_use_id}")
                        log(f"      is_error: {block.is_error}")
                        log(f"      content: {content_preview}...")
            else:
                log(f"  [{idx}] UserMessage: {str(msg.content)[:150]}")

        elif isinstance(msg, ResultMessage):
            log(f"  [{idx}] ResultMessage")
            log(f"      session_id: {msg.session_id}")
            log(f"      num_turns: {msg.num_turns}")
            log(f"      cost: ${msg.total_cost_usd or 0:.6f}")
            log(f"      duration_ms: {msg.duration_ms}")
            log(f"      is_error: {msg.is_error}")
            log(f"      usage: {msg.usage}")

        elif isinstance(msg, SystemMessage):
            log(f"  [{idx}] SystemMessage: subtype={msg.subtype}")

        message_flow.append(msg_type)

    log.sep("Tool Call Summary")
    log(f"Total messages: {len(message_flow)}")
    log(f"Total tool calls: {len(tool_calls)}")
    for i, tc in enumerate(tool_calls):
        log(f"  Tool {i+1}: {tc['name']} (id={tc['id'][:25]}...)")
    log(f"Message flow: {message_flow}")


async def test_plan_mode(log):
    """Test permission_mode='plan' — does agent plan but not execute?"""
    from claude_agent_sdk import (
        query, ClaudeAgentOptions, AssistantMessage, ResultMessage,
        TextBlock, ToolUseBlock,
    )

    log.sep("Part B: permission_mode='plan'")

    options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=3,
        allowed_tools=["Read", "Bash"],
        permission_mode="plan",
        cwd=str(PROJECT_ROOT),
    )

    tool_calls_seen = 0
    text_output = ""

    async for msg in query(
        prompt="Read pyproject.toml and tell me what Python version this project requires.",
        options=options,
    ):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    tool_calls_seen += 1
                    log(f"  Tool call attempted: {block.name}")
                elif isinstance(block, TextBlock):
                    text_output += block.text
        elif isinstance(msg, ResultMessage):
            log(f"  num_turns: {msg.num_turns}, cost: ${msg.total_cost_usd or 0:.6f}, is_error: {msg.is_error}")

    log(f"Tool calls seen: {tool_calls_seen}")
    log(f"Response: {text_output[:400]}")
    log(f"Plan mode prevents tool execution: {'Yes' if tool_calls_seen == 0 else 'No (tools were called!)'}")


async def test_max_turns_limit(log):
    """Test what happens when max_turns is reached."""
    from claude_agent_sdk import (
        query, ClaudeAgentOptions, AssistantMessage, ResultMessage,
        TextBlock, ToolUseBlock,
    )

    log.sep("Part C: max_turns=1 limit behavior")

    options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=1,
        allowed_tools=["Read", "Bash"],
        permission_mode="bypassPermissions",
        cwd=str(PROJECT_ROOT),
    )

    tool_calls = 0
    text_output = ""

    async for msg in query(
        prompt="Read pyproject.toml and tell me about the dependencies. Then read README.md too.",
        options=options,
    ):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    tool_calls += 1
                    log(f"  Tool call {tool_calls}: {block.name}")
                elif isinstance(block, TextBlock):
                    text_output += block.text
        elif isinstance(msg, ResultMessage):
            log(f"  num_turns used: {msg.num_turns}, is_error: {msg.is_error}")
            log(f"  result preview: {msg.result[:200] if msg.result else 'None'}")

    log(f"Total tool calls: {tool_calls}")
    log(f"Response preview: {text_output[:300]}")


async def main():
    load_env()
    log = Log("exp_04_output")

    log.sep("Experiment 04: Built-in Tool Usage & Permission Modes")

    await test_tool_usage(log)
    await test_plan_mode(log)
    await test_max_turns_limit(log)

    log.sep("Key Takeaways")
    log("1. What is the message sequence for tool-using queries?")
    log("2. How does max_turns interact with tool calls?")
    log("3. Does permission_mode='plan' prevent tool execution?")
    log("4. Does bypassPermissions work fully headless?")

    log.close()


if __name__ == "__main__":
    asyncio.run(main())
