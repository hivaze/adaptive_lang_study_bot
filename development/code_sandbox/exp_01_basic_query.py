"""
Experiment 01: Basic Query & Message Anatomy
=============================================
Goal: Understand query() API, message types, content blocks, cost/usage metadata.

Run: poetry run python development/code_sandbox/exp_01_basic_query.py
Output saved to: development/code_sandbox/output/exp_01_output.txt
"""

import asyncio
import os
import sys
import io
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_DIR = Path(__file__).resolve().parent / "output"


def load_env():
    os.environ.pop("CLAUDECODE", None)
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())


class Log:
    """Dual output: file + stderr (stdout captured by parent CLI process)."""

    def __init__(self, name: str):
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        self._file = open(OUTPUT_DIR / f"{name}.txt", "w")

    def __call__(self, *args, **kwargs):
        buf = io.StringIO()
        print(*args, file=buf, **kwargs)
        text = buf.getvalue()
        self._file.write(text)
        self._file.flush()
        sys.stderr.write(text)
        sys.stderr.flush()

    def sep(self, title: str):
        self(f"\n{'=' * 60}")
        self(f"  {title}")
        self(f"{'=' * 60}\n")

    def close(self):
        self._file.close()


async def main():
    load_env()
    log = Log("exp_01_output")

    from claude_agent_sdk import (
        query,
        ClaudeAgentOptions,
        AssistantMessage,
        ResultMessage,
        SystemMessage,
        UserMessage,
        TextBlock,
        ThinkingBlock,
        ToolUseBlock,
        ToolResultBlock,
        CLINotFoundError,
        ProcessError,
        ClaudeSDKError,
    )
    from claude_agent_sdk.types import StreamEvent

    log.sep("Experiment 01: Basic Query & Message Anatomy")

    options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        max_turns=1,
    )

    log(f"Options: model={options.model}, max_turns={options.max_turns}")
    log(f"Prompt: 'Explain what you are and what model you're using, in 2 sentences.'")
    log()

    message_count = 0
    message_types_seen = []

    try:
        async for msg in query(
            prompt="Explain what you are and what model you're using, in 2 sentences.",
            options=options,
        ):
            message_count += 1
            msg_type = type(msg).__name__
            message_types_seen.append(msg_type)

            log.sep(f"Message #{message_count}: {msg_type}")

            if isinstance(msg, AssistantMessage):
                log(f"  model: {msg.model}")
                log(f"  content blocks ({len(msg.content)}):")
                for i, block in enumerate(msg.content):
                    block_type = type(block).__name__
                    log(f"    [{i}] {block_type}")
                    if isinstance(block, TextBlock):
                        log(f"        text: {block.text[:500]}{'...' if len(block.text) > 500 else ''}")
                    elif isinstance(block, ThinkingBlock):
                        log(f"        thinking: {block.thinking[:300]}{'...' if len(block.thinking) > 300 else ''}")
                        log(f"        signature: {block.signature[:50]}...")
                    elif isinstance(block, ToolUseBlock):
                        log(f"        id: {block.id}")
                        log(f"        name: {block.name}")
                        log(f"        input: {block.input}")
                    elif isinstance(block, ToolResultBlock):
                        log(f"        tool_use_id: {block.tool_use_id}")
                        content_str = str(block.content)[:200] if block.content else "None"
                        log(f"        content: {content_str}")
                        log(f"        is_error: {block.is_error}")

            elif isinstance(msg, ResultMessage):
                log(f"  subtype: {msg.subtype}")
                log(f"  session_id: {msg.session_id}")
                log(f"  is_error: {msg.is_error}")
                log(f"  num_turns: {msg.num_turns}")
                log(f"  duration_ms: {msg.duration_ms}")
                log(f"  duration_api_ms: {msg.duration_api_ms}")
                log(f"  total_cost_usd: {msg.total_cost_usd}")
                log(f"  result: {msg.result[:500] if msg.result else 'None'}")
                log(f"  usage: {msg.usage}")

            elif isinstance(msg, SystemMessage):
                log(f"  subtype: {msg.subtype}")
                data_keys = list(msg.data.keys()) if isinstance(msg.data, dict) else str(msg.data)[:200]
                log(f"  data keys: {data_keys}")

            elif isinstance(msg, UserMessage):
                content_str = str(msg.content)[:200] if msg.content else "None"
                log(f"  content: {content_str}")

            elif isinstance(msg, StreamEvent):
                log(f"  uuid: {msg.uuid}")
                log(f"  session_id: {msg.session_id}")
                event_type = msg.event.get("type", "unknown") if isinstance(msg.event, dict) else "unknown"
                log(f"  event type: {event_type}")

            else:
                log(f"  OTHER: {type(msg).__name__}: {repr(msg)[:300]}")

    except CLINotFoundError as e:
        log(f"ERROR: Claude CLI not found: {e}")
    except ProcessError as e:
        log(f"ERROR: Process error (exit_code={e.exit_code}): {e}")
    except ClaudeSDKError as e:
        log(f"ERROR: SDK error: {e}")
    except Exception as e:
        log(f"ERROR: {type(e).__name__}: {e}")

    log.sep("Summary")
    log(f"Total messages received: {message_count}")
    log(f"Message types in order: {message_types_seen}")
    log(f"Unique types: {list(dict.fromkeys(message_types_seen))}")

    log.close()


if __name__ == "__main__":
    asyncio.run(main())
