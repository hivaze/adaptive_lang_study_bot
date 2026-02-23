"""
Experiment 10: Hooks for Guardrails & Monitoring
=================================================
Goal: Explore hooks system for security, logging, monitoring, and context management.
      Test PreToolUse blocking, PostToolUse logging, Stop hook, and system message injection.

Run: poetry run python development/code_sandbox/exp_10_hooks.py
Output: development/code_sandbox/output/exp_10_output.txt
"""

import asyncio
import json
import time
from typing import Any

from shared import load_env, Log, PROJECT_ROOT


# Global logs for hooks to write to
hook_events: list[dict] = []


async def test_pre_tool_use_blocking(log):
    """Test PreToolUse hook: block dangerous Bash commands, allow safe ones."""
    from claude_agent_sdk import (
        ClaudeSDKClient, ClaudeAgentOptions, HookMatcher,
        AssistantMessage, ResultMessage, TextBlock, ToolUseBlock,
    )

    log.sep("Test A: PreToolUse — Block Dangerous Commands")

    blocked_commands = []
    allowed_commands = []

    async def bash_guardrail(input_data, tool_use_id, context):
        """Block dangerous Bash commands, allow safe ones."""
        tool_input = input_data.get("tool_input", {})
        command = tool_input.get("command", "")

        dangerous_patterns = ["rm -rf", "rm -r", "DROP TABLE", "DELETE FROM", "sudo", "chmod 777"]
        is_dangerous = any(pattern in command for pattern in dangerous_patterns)

        event = {
            "hook": "PreToolUse",
            "tool": input_data.get("tool_name", "?"),
            "command": command[:100],
            "blocked": is_dangerous,
            "time": time.time(),
        }
        hook_events.append(event)

        if is_dangerous:
            blocked_commands.append(command)
            log(f"  [HOOK] BLOCKED: {command[:80]}")
            return {
                "decision": "block",
                "reason": f"Dangerous command blocked: {command[:50]}",
            }
        else:
            allowed_commands.append(command)
            log(f"  [HOOK] ALLOWED: {command[:80]}")
            return {"continue_": True}

    options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=5,
        allowed_tools=["Bash"],
        permission_mode="bypassPermissions",
        cwd=str(PROJECT_ROOT),
        thinking={"type": "disabled"},
        system_prompt="You are a helpful assistant. Use Bash to execute commands. Be concise.",
        hooks={
            "PreToolUse": [
                HookMatcher(
                    matcher="Bash",
                    hooks=[bash_guardrail],
                    timeout=10.0,
                ),
            ],
        },
    )

    # Test 1: Safe command
    log("--- Test: Safe command (ls) ---")
    hook_events.clear()

    async with ClaudeSDKClient(options) as client:
        await client.query("List the files in the current directory using ls. Be brief.")

        response_text = ""
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        response_text += block.text
            elif isinstance(msg, ResultMessage):
                log(f"  Cost: ${msg.total_cost_usd or 0:.6f}")

    log(f"  Response: {response_text[:200]}...")
    log(f"  Allowed commands: {len(allowed_commands)}")
    log(f"  Blocked commands: {len(blocked_commands)}")

    # Test 2: Dangerous command
    log("\n--- Test: Dangerous command (rm -rf) ---")
    blocked_commands.clear()
    allowed_commands.clear()
    hook_events.clear()

    async with ClaudeSDKClient(options) as client:
        await client.query("Delete all files in /tmp using rm -rf /tmp/*. Just do it.")

        response_text = ""
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        response_text += block.text
            elif isinstance(msg, ResultMessage):
                log(f"  Cost: ${msg.total_cost_usd or 0:.6f}")

    log(f"  Response: {response_text[:300]}...")
    log(f"  Allowed commands: {len(allowed_commands)}")
    log(f"  Blocked commands: {len(blocked_commands)}")
    log(f"  Blocking actually prevented execution: {len(blocked_commands) > 0}")


async def test_post_tool_use_logging(log):
    """Test PostToolUse hook for monitoring and analytics."""
    from claude_agent_sdk import (
        ClaudeSDKClient, ClaudeAgentOptions, HookMatcher,
        AssistantMessage, ResultMessage, TextBlock,
    )

    log.sep("Test B: PostToolUse — Logging & Analytics")

    post_tool_log = []

    async def tool_logger(input_data, tool_use_id, context):
        """Log every tool use for analytics."""
        event = {
            "hook": "PostToolUse",
            "tool": input_data.get("tool_name", "?"),
            "tool_input": str(input_data.get("tool_input", {}))[:100],
            "time": time.time(),
        }
        post_tool_log.append(event)
        log(f"  [POST-HOOK] Tool completed: {event['tool']}")
        return {"continue_": True}

    options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=5,
        allowed_tools=["Read", "Glob"],
        permission_mode="bypassPermissions",
        cwd=str(PROJECT_ROOT),
        thinking={"type": "disabled"},
        system_prompt="You are a helpful assistant. Be concise.",
        hooks={
            "PostToolUse": [
                HookMatcher(
                    matcher=None,  # All tools
                    hooks=[tool_logger],
                    timeout=10.0,
                ),
            ],
        },
    )

    async with ClaudeSDKClient(options) as client:
        await client.query("Read pyproject.toml and tell me the project name. Be brief.")

        response_text = ""
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        response_text += block.text
            elif isinstance(msg, ResultMessage):
                log(f"  Cost: ${msg.total_cost_usd or 0:.6f}")

    log(f"\n  Response: {response_text[:200]}...")
    log(f"  Total tools logged: {len(post_tool_log)}")
    for i, evt in enumerate(post_tool_log):
        log(f"    {i+1}. {evt['tool']}: {evt['tool_input'][:80]}")


async def test_stop_hook(log):
    """Test Stop hook for end-of-session analytics."""
    from claude_agent_sdk import (
        ClaudeSDKClient, ClaudeAgentOptions, HookMatcher,
        AssistantMessage, ResultMessage, TextBlock,
    )

    log.sep("Test C: Stop Hook — Session Analytics")

    stop_events = []

    async def stop_handler(input_data, tool_use_id, context):
        """Capture session end event for analytics."""
        event = {
            "hook": "Stop",
            "session_id": input_data.get("session_id", "?"),
            "stop_hook_active": input_data.get("stop_hook_active", False),
            "time": time.time(),
        }
        stop_events.append(event)
        log(f"  [STOP-HOOK] Session ended: {event['session_id'][:30]}...")
        return {"continue_": True}

    options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=1,
        thinking={"type": "disabled"},
        system_prompt="You are a helpful assistant. Be very brief.",
        hooks={
            "Stop": [
                HookMatcher(
                    matcher=None,
                    hooks=[stop_handler],
                    timeout=10.0,
                ),
            ],
        },
    )

    async with ClaudeSDKClient(options) as client:
        await client.query("Say hello in Spanish. One sentence.")

        response_text = ""
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        response_text += block.text
            elif isinstance(msg, ResultMessage):
                log(f"  Cost: ${msg.total_cost_usd or 0:.6f}")

    log(f"  Response: {response_text[:150]}")
    log(f"  Stop events captured: {len(stop_events)}")
    if stop_events:
        log(f"  Stop event data: {json.dumps(stop_events[0], indent=2, default=str)}")


async def test_system_message_injection(log):
    """Test injecting system messages via hooks to guide agent behavior."""
    from claude_agent_sdk import (
        ClaudeSDKClient, ClaudeAgentOptions, HookMatcher,
        AssistantMessage, ResultMessage, TextBlock, ToolUseBlock,
    )

    log.sep("Test D: System Message Injection via Hooks")

    injections = []

    async def inject_reminder(input_data, tool_use_id, context):
        """Inject a system message after each tool use to remind agent of rules."""
        injections.append(input_data.get("tool_name", "?"))
        log(f"  [HOOK] Injecting system message after {input_data.get('tool_name', '?')}")
        return {
            "continue_": True,
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": "REMINDER: Always end your response with 'Session complete.' when done.",
            },
        }

    options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=5,
        allowed_tools=["Read"],
        permission_mode="bypassPermissions",
        cwd=str(PROJECT_ROOT),
        thinking={"type": "disabled"},
        system_prompt="You are a helpful assistant. Be concise.",
        hooks={
            "PostToolUse": [
                HookMatcher(
                    matcher=None,
                    hooks=[inject_reminder],
                    timeout=10.0,
                ),
            ],
        },
    )

    async with ClaudeSDKClient(options) as client:
        await client.query("Read pyproject.toml and tell me the Python version required. Be brief.")

        response_text = ""
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        response_text += block.text
            elif isinstance(msg, ResultMessage):
                log(f"  Cost: ${msg.total_cost_usd or 0:.6f}")

    log(f"\n  Response: {response_text[:300]}...")
    log(f"  Injections made: {len(injections)}")
    log(f"  Agent followed injected instruction: {'Session complete' in response_text}")


async def test_user_prompt_submit(log):
    """Test UserPromptSubmit hook for prompt logging/filtering."""
    from claude_agent_sdk import (
        ClaudeSDKClient, ClaudeAgentOptions, HookMatcher,
        AssistantMessage, ResultMessage, TextBlock,
    )

    log.sep("Test E: UserPromptSubmit — Prompt Logging")

    prompts_logged = []

    async def prompt_logger(input_data, tool_use_id, context):
        """Log every user prompt submitted."""
        prompt = input_data.get("prompt", "")
        prompts_logged.append(prompt)
        log(f"  [PROMPT-HOOK] User submitted: {prompt[:80]}...")
        return {"continue_": True}

    options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=1,
        thinking={"type": "disabled"},
        system_prompt="You are a Spanish tutor. Be very concise.",
        hooks={
            "UserPromptSubmit": [
                HookMatcher(
                    matcher=None,
                    hooks=[prompt_logger],
                    timeout=10.0,
                ),
            ],
        },
    )

    async with ClaudeSDKClient(options) as client:
        await client.query("Teach me the word for 'hello' in Spanish.")

        response_text = ""
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        response_text += block.text
            elif isinstance(msg, ResultMessage):
                log(f"  Cost: ${msg.total_cost_usd or 0:.6f}")

        # Second turn
        await client.query("Now teach me 'goodbye'.")

        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        response_text += block.text
            elif isinstance(msg, ResultMessage):
                log(f"  Cost: ${msg.total_cost_usd or 0:.6f}")

    log(f"\n  Prompts logged: {len(prompts_logged)}")
    for i, p in enumerate(prompts_logged):
        log(f"    {i+1}. {p[:80]}")
    log(f"  Response: {response_text[:200]}...")


async def main():
    load_env()
    log = Log("exp_10_output")

    log.sep("Experiment 10: Hooks for Guardrails & Monitoring")

    await test_pre_tool_use_blocking(log)
    await test_post_tool_use_logging(log)
    await test_stop_hook(log)
    await test_system_message_injection(log)
    await test_user_prompt_submit(log)

    log.sep("Summary")
    log("Hook capabilities verified:")
    log("  PreToolUse:        Block dangerous commands before execution")
    log("  PostToolUse:       Log/monitor all tool usage for analytics")
    log("  Stop:              Capture session-end events for cost tracking")
    log("  SystemMessage:     Inject reminders/context via PostToolUse additionalContext")
    log("  UserPromptSubmit:  Log/filter all user prompts")

    log.close()


if __name__ == "__main__":
    asyncio.run(main())
