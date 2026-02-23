"""Unit tests for run_proactive_llm_session().

All SDK and infrastructure calls are mocked — no DB, Redis, or Claude API needed.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import ResultMessage

from adaptive_lang_study_bot.agent.session_manager import run_proactive_llm_session


def _make_user(**overrides):
    user = MagicMock()
    user.telegram_id = 123
    user.first_name = "Alex"
    user.native_language = "en"
    user.target_language = "fr"
    user.level = "A2"
    user.streak_days = 12
    user.vocabulary_count = 340
    user.interests = ["cooking"]
    user.learning_goals = []
    user.weak_areas = []
    user.recent_scores = [7, 8]
    user.topics_to_avoid = []
    user.tier = "free"
    user.timezone = "UTC"
    for k, v in overrides.items():
        setattr(user, k, v)
    return user


def _patch_infrastructure():
    """Return a dict of patches for all infrastructure dependencies."""
    return {
        "pool_acquire": patch(
            "adaptive_lang_study_bot.agent.session_manager.session_pool.acquire_proactive",
            new_callable=AsyncMock,
            return_value=True,
        ),
        "pool_release": patch(
            "adaptive_lang_study_bot.agent.session_manager.session_pool.release_proactive",
            new_callable=AsyncMock,
        ),
        "lock_acquire": patch(
            "adaptive_lang_study_bot.agent.session_manager.acquire_session_lock",
            new_callable=AsyncMock,
            return_value="test-lock-token",
        ),
        "lock_release": patch(
            "adaptive_lang_study_bot.agent.session_manager.release_session_lock",
            new_callable=AsyncMock,
        ),
        "session_factory": patch(
            "adaptive_lang_study_bot.agent.session_manager.async_session_factory",
        ),
        "session_repo_create": patch(
            "adaptive_lang_study_bot.agent.session_manager.SessionRepo.create",
            new_callable=AsyncMock,
        ),
        "session_repo_update": patch(
            "adaptive_lang_study_bot.agent.session_manager.SessionRepo.update_end",
            new_callable=AsyncMock,
        ),
        "create_tools": patch(
            "adaptive_lang_study_bot.agent.session_manager.create_session_tools",
        ),
        "create_server": patch(
            "adaptive_lang_study_bot.agent.session_manager.create_langbot_server",
            return_value=MagicMock(),
        ),
        "build_prompt": patch(
            "adaptive_lang_study_bot.agent.session_manager.build_proactive_prompt",
            return_value="System prompt",
        ),
        "sdk_client_cls": patch(
            "adaptive_lang_study_bot.agent.session_manager.ClaudeSDKClient",
        ),
        "pop_env": patch(
            "os.environ.pop",
        ),
    }


def _setup_tools_mock(mock_create_tools, notification_messages=None):
    """Configure the create_session_tools mock to populate notification_sink."""
    tool_a = MagicMock()
    tool_a.name = "get_user_profile"
    tool_b = MagicMock()
    tool_b.name = "send_notification"

    def side_effect(*, session_factory, user_id, session_id, session_type, user_timezone, notification_sink=None):
        if notification_sink is not None and notification_messages:
            notification_sink.extend(notification_messages)
        can_use = lambda name: True
        return [tool_a, tool_b], can_use

    mock_create_tools.side_effect = side_effect


class _AsyncIterator:
    """Async iterator wrapper for mock responses."""

    def __init__(self, items):
        self._items = list(items)
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._index]
        self._index += 1
        return item


def _setup_sdk_mock(mock_sdk_cls, cost=0.002):
    """Configure the ClaudeSDKClient mock."""
    mock_client = AsyncMock()
    mock_sdk_cls.return_value = mock_client

    # ResultMessage mock — spec= so isinstance() check passes
    result_msg = MagicMock(spec=ResultMessage)
    result_msg.total_cost_usd = cost

    # receive_response must be a plain MagicMock so calling it returns
    # the async iterator directly, not a coroutine wrapping it.
    mock_client.receive_response = MagicMock(return_value=_AsyncIterator([result_msg]))
    return mock_client


def _setup_session_factory(mock_factory):
    """Configure async_session_factory mock for both context manager and raw usage."""
    mock_db = AsyncMock()
    # Context manager usage (async with async_session_factory() as db)
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_db)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
    # Raw usage (tool_db_session = async_session_factory(); await tool_db_session.__aenter__())
    mock_factory.return_value.rollback = AsyncMock()
    return mock_db


class TestRunProactiveLLMSession:

    @pytest.mark.asyncio
    async def test_pool_full_returns_none(self):
        """When proactive pool is full, returns (None, 0.0) immediately."""
        user = _make_user()

        with patch(
            "adaptive_lang_study_bot.agent.session_manager.session_pool.acquire_proactive",
            new_callable=AsyncMock,
            return_value=False,
        ):
            message, cost = await run_proactive_llm_session(
                user, "proactive_nudge", {"streak": 12},
            )

        assert message is None
        assert cost == 0.0

    @pytest.mark.asyncio
    async def test_successful_session_returns_message(self):
        """Successful LLM session returns the notification message and cost."""
        user = _make_user()
        patches = _patch_infrastructure()

        with patches["pool_acquire"], patches["pool_release"] as mock_release, \
             patches["lock_acquire"], patches["lock_release"], \
             patches["session_factory"] as mock_factory, \
             patches["session_repo_create"], patches["session_repo_update"], \
             patches["create_tools"] as mock_tools, \
             patches["create_server"], patches["build_prompt"], \
             patches["sdk_client_cls"] as mock_sdk_cls, patches["pop_env"]:

            _setup_session_factory(mock_factory)
            _setup_tools_mock(mock_tools, notification_messages=["Hello Alex!"])
            _setup_sdk_mock(mock_sdk_cls, cost=0.002)

            message, cost = await run_proactive_llm_session(
                user, "proactive_nudge", {"streak": 12},
            )

        assert message == "Hello Alex!"
        assert cost == 0.002
        mock_release.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_send_notification_returns_none(self):
        """When send_notification is never called, returns (None, cost)."""
        user = _make_user()
        patches = _patch_infrastructure()

        with patches["pool_acquire"], patches["pool_release"] as mock_release, \
             patches["lock_acquire"], patches["lock_release"], \
             patches["session_factory"] as mock_factory, \
             patches["session_repo_create"], patches["session_repo_update"], \
             patches["create_tools"] as mock_tools, \
             patches["create_server"], patches["build_prompt"], \
             patches["sdk_client_cls"] as mock_sdk_cls, patches["pop_env"]:

            _setup_session_factory(mock_factory)
            _setup_tools_mock(mock_tools, notification_messages=None)
            _setup_sdk_mock(mock_sdk_cls, cost=0.001)

            message, cost = await run_proactive_llm_session(
                user, "proactive_nudge", {},
            )

        assert message is None
        mock_release.assert_called_once()

    @pytest.mark.asyncio
    async def test_sdk_error_returns_none_and_releases_pool(self):
        """On SDK error, returns (None, 0.0) and releases pool slot."""
        user = _make_user()
        patches = _patch_infrastructure()

        with patches["pool_acquire"], patches["pool_release"] as mock_release, \
             patches["lock_acquire"], patches["lock_release"], \
             patches["session_factory"] as mock_factory, \
             patches["session_repo_create"], patches["session_repo_update"], \
             patches["create_tools"] as mock_tools, \
             patches["create_server"], patches["build_prompt"], \
             patches["sdk_client_cls"] as mock_sdk_cls, patches["pop_env"]:

            _setup_session_factory(mock_factory)
            _setup_tools_mock(mock_tools)

            mock_client = AsyncMock()
            mock_sdk_cls.return_value = mock_client
            mock_client.__aenter__.side_effect = RuntimeError("SDK failed to start")

            message, cost = await run_proactive_llm_session(
                user, "proactive_nudge", {},
            )

        assert message is None
        assert cost == 0.0
        mock_release.assert_called_once()

    @pytest.mark.asyncio
    async def test_pool_always_released_on_success(self):
        """Pool slot is released even after successful session."""
        user = _make_user()
        patches = _patch_infrastructure()

        with patches["pool_acquire"], patches["pool_release"] as mock_release, \
             patches["lock_acquire"], patches["lock_release"], \
             patches["session_factory"] as mock_factory, \
             patches["session_repo_create"], patches["session_repo_update"], \
             patches["create_tools"] as mock_tools, \
             patches["create_server"], patches["build_prompt"], \
             patches["sdk_client_cls"] as mock_sdk_cls, patches["pop_env"]:

            _setup_session_factory(mock_factory)
            _setup_tools_mock(mock_tools, notification_messages=["msg"])
            _setup_sdk_mock(mock_sdk_cls)

            await run_proactive_llm_session(user, "proactive_review", {"due_count": 5})

        mock_release.assert_called_once()
