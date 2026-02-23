"""Shared utilities for all experiments."""

import io
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_DIR = Path(__file__).resolve().parent / "output"


def load_env():
    """Load .env and unset CLAUDECODE to allow nested SDK calls."""
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


def extract_text(messages: list) -> str:
    """Extract all text content from a list of messages."""
    from claude_agent_sdk import AssistantMessage, TextBlock

    text = ""
    for msg in messages:
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    text += block.text
    return text


def extract_result(messages: list) -> dict:
    """Extract ResultMessage metadata from a list of messages."""
    from claude_agent_sdk import ResultMessage

    for msg in messages:
        if isinstance(msg, ResultMessage):
            return {
                "session_id": msg.session_id,
                "cost": msg.total_cost_usd or 0.0,
                "duration_ms": msg.duration_ms,
                "duration_api_ms": msg.duration_api_ms,
                "num_turns": msg.num_turns,
                "is_error": msg.is_error,
                "usage": msg.usage,
                "result": msg.result,
            }
    return {}
