"""LLM test fixtures — real Claude Haiku 4.5 calls against testcontainer DB."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator, Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from adaptive_lang_study_bot.agent.hooks import SessionHookState, build_session_hooks
from adaptive_lang_study_bot.agent.prompt_builder import (
    build_system_prompt,
    compute_session_context,
)
from adaptive_lang_study_bot.agent.tools import (
    create_langbot_server,
    create_session_tools,
)
from adaptive_lang_study_bot.db.models import Session as DBSession, User


# ---------------------------------------------------------------------------
# Session-scoped guards
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    """Load .env file into os.environ (same pattern as development/code_sandbox/shared.py)."""
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())


@pytest.fixture(scope="session", autouse=True)
def require_anthropic_key():
    """Load .env and skip all LLM tests if ANTHROPIC_API_KEY is not set."""
    _load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set, skipping LLM tests")


@pytest.fixture(scope="session", autouse=True)
def unset_claudecode():
    """Unset CLAUDECODE env var to allow nested SDK calls in tests."""
    os.environ.pop("CLAUDECODE", None)
    yield


# ---------------------------------------------------------------------------
# DB session (committing — tools call commit() internally)
# ---------------------------------------------------------------------------

@pytest.fixture()
async def llm_db_session(pg_url: str) -> AsyncGenerator[AsyncSession, None]:
    """Provide a committing DB session for LLM tool closures.

    Unlike the integration db_session (which rolls back via savepoints),
    tools call commit() internally.  After the test, we delete all user
    data (cascade handles related tables).
    """
    engine = create_async_engine(pg_url)
    session = AsyncSession(bind=engine, expire_on_commit=False)
    try:
        yield session
    finally:
        # Cleanup: cascade-delete all test users
        await session.execute(delete(User))
        await session.commit()
        await session.close()
        await engine.dispose()


# ---------------------------------------------------------------------------
# User factory
# ---------------------------------------------------------------------------

_LLM_USER_COUNTER = 200_000


@pytest.fixture()
def make_llm_user(
    llm_db_session: AsyncSession,
) -> Callable[..., Coroutine[Any, Any, User]]:
    """Factory fixture to create a User with sensible defaults for LLM tests."""

    async def _factory(**overrides: Any) -> User:
        global _LLM_USER_COUNTER
        _LLM_USER_COUNTER += 1
        defaults: dict[str, Any] = {
            "telegram_id": _LLM_USER_COUNTER,
            "first_name": "TestStudent",
            "native_language": "en",
            "target_language": "es",
            "level": "A2",
            "onboarding_completed": True,
            "interests": ["cooking", "travel"],
            "learning_goals": [],
            "preferred_difficulty": "normal",
            "session_style": "structured",
            "streak_days": 5,
            "vocabulary_count": 0,
            "sessions_completed": 10,
            "weak_areas": [],
            "strong_areas": [],
            "recent_scores": [],
            "topics_to_avoid": [],
        }
        defaults.update(overrides)
        user = User(**defaults)
        llm_db_session.add(user)
        await llm_db_session.commit()
        return user

    return _factory


# ---------------------------------------------------------------------------
# LLM test session
# ---------------------------------------------------------------------------

@dataclass
class LLMTestSession:
    """Wraps a ClaudeSDKClient with helpers for test assertions."""

    client: ClaudeSDKClient
    hook_state: SessionHookState
    user: User
    db_session: AsyncSession
    tools_called: list[str] = field(default_factory=list)
    response_text: str = ""
    cost: float = 0.0

    async def query_and_collect(self, prompt: str) -> list[str]:
        """Send a query and collect response text chunks + tool calls."""
        chunks: list[str] = []
        await self.client.query(prompt)
        async for msg in self.client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
                        self.response_text += block.text
                    elif isinstance(block, ToolUseBlock):
                        self.tools_called.append(block.name)
            elif isinstance(msg, ResultMessage):
                self.cost += msg.total_cost_usd or 0
                if msg.num_turns is not None:
                    self.hook_state.turn_count = msg.num_turns

        # Refresh user from DB so assertions see tool-committed changes
        # (tools use raw SQL updates that bypass the ORM identity map)
        try:
            await self.db_session.refresh(self.user)
        except Exception:
            pass  # user may have been deleted or session invalidated

        return chunks

    @property
    def bare_tools(self) -> list[str]:
        """Tool names without the MCP prefix."""
        return [name.removeprefix("mcp__langbot__") for name in self.tools_called]


@pytest.fixture()
async def create_llm_session(
    llm_db_session: AsyncSession,
    make_llm_user: Callable[..., Coroutine[Any, Any, User]],
):
    """Factory fixture: creates a real LLM session with production code paths.

    The returned coroutine builds ClaudeSDKClient using the actual
    create_session_tools(), build_system_prompt(), build_session_hooks()
    from production, pointed at the testcontainer DB.
    """
    _sessions: list[LLMTestSession] = []

    async def _factory(
        *,
        user_overrides: dict[str, Any] | None = None,
        session_type: str = "interactive",
        max_turns: int = 5,
        system_prompt_override: str | None = None,
    ) -> LLMTestSession:
        user = await make_llm_user(**(user_overrides or {}))

        # Create a session record (exercise_results FK → sessions)
        session_id = str(uuid.uuid4())
        db_session_record = DBSession(
            id=uuid.UUID(session_id),
            user_id=user.telegram_id,
            session_type=session_type,
        )
        llm_db_session.add(db_session_record)
        await llm_db_session.commit()

        # Production tool creation — pass a factory that yields the shared
        # test session (tools open/close per-call in production, but in tests
        # we share a single session so FK constraints stay visible).
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _test_session_factory():
            yield llm_db_session

        all_tools, can_use_tool = create_session_tools(
            session_factory=_test_session_factory,
            user_id=user.telegram_id,
            session_id=session_id,
            session_type=session_type,
        )

        # Primary filter: exclude disallowed tools from the MCP server
        # (mirrors session_manager.py)
        tools = [t for t in all_tools if can_use_tool(t.name)]
        allowed_tool_names = [
            f"mcp__langbot__{t.name}" for t in tools
        ]

        hooks, hook_state = build_session_hooks(user.telegram_id)
        server = create_langbot_server(tools)

        # System prompt: production builder or custom override
        if system_prompt_override:
            system_prompt = system_prompt_override
        else:
            session_ctx = compute_session_context(user)
            system_prompt = build_system_prompt(user, session_ctx, due_count=0)

        options = ClaudeAgentOptions(
            model="claude-haiku-4-5",
            max_turns=max_turns,
            thinking={"type": "disabled"},
            mcp_servers={"langbot": server},
            allowed_tools=allowed_tool_names,
            permission_mode="bypassPermissions",
            system_prompt=system_prompt,
            hooks=hooks,
        )

        client = ClaudeSDKClient(options)
        await client.__aenter__()

        hook_state.max_turns = max_turns

        session = LLMTestSession(
            client=client,
            hook_state=hook_state,
            user=user,
            db_session=llm_db_session,
            tools_called=[],
        )
        _sessions.append(session)
        return session

    yield _factory

    # Cleanup: close all SDK clients after the test
    for session in _sessions:
        try:
            await session.client.__aexit__(None, None, None)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Helpers (importable by test modules)
# ---------------------------------------------------------------------------

def strip_tools(tools_called: list[str]) -> list[str]:
    """Strip MCP prefix from tool names for readable assertions."""
    return [name.removeprefix("mcp__langbot__") for name in tools_called]
