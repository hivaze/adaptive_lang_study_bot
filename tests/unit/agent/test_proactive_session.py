"""Unit tests for run_proactive_llm_session().

All SDK and infrastructure calls are mocked — no DB, Redis, or Claude API needed.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

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
    user.additional_notes = []
    user.tier = "free"
    user.timezone = "UTC"
    user.field_timestamps = {}
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


def _setup_sdk_mock(mock_sdk_cls, text_output="Hello Alex!", cost=0.002):
    """Configure the ClaudeSDKClient mock with TextBlock output."""
    mock_client = AsyncMock()
    mock_sdk_cls.return_value = mock_client

    messages = []

    if text_output is not None:
        # AssistantMessage with TextBlock — spec= so isinstance() checks pass
        text_block = MagicMock(spec=TextBlock)
        text_block.text = text_output
        assistant_msg = MagicMock(spec=AssistantMessage)
        assistant_msg.content = [text_block]
        messages.append(assistant_msg)

    # ResultMessage mock
    result_msg = MagicMock(spec=ResultMessage)
    result_msg.total_cost_usd = cost
    result_msg.num_turns = 1
    messages.append(result_msg)

    # receive_response must be a plain MagicMock so calling it returns
    # the async iterator directly, not a coroutine wrapping it.
    mock_client.receive_response = MagicMock(return_value=_AsyncIterator(messages))
    return mock_client


def _setup_session_factory(mock_factory):
    """Configure async_session_factory mock for context manager usage."""
    mock_db = AsyncMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_db)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
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
        """Successful LLM session returns the notification text and cost."""
        user = _make_user()
        patches = _patch_infrastructure()

        with patches["pool_acquire"], patches["pool_release"] as mock_release, \
             patches["lock_acquire"], patches["lock_release"], \
             patches["session_factory"] as mock_factory, \
             patches["session_repo_create"], patches["session_repo_update"], \
             patches["build_prompt"], \
             patches["sdk_client_cls"] as mock_sdk_cls, patches["pop_env"]:

            _setup_session_factory(mock_factory)
            _setup_sdk_mock(mock_sdk_cls, text_output="Hello Alex!", cost=0.002)

            message, cost = await run_proactive_llm_session(
                user, "proactive_nudge", {"streak": 12},
            )

        assert message == "Hello Alex!"
        assert cost == 0.002
        mock_release.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_response_returns_none(self):
        """When LLM outputs no text, returns (None, cost)."""
        user = _make_user()
        patches = _patch_infrastructure()

        with patches["pool_acquire"], patches["pool_release"] as mock_release, \
             patches["lock_acquire"], patches["lock_release"], \
             patches["session_factory"] as mock_factory, \
             patches["session_repo_create"], patches["session_repo_update"], \
             patches["build_prompt"], \
             patches["sdk_client_cls"] as mock_sdk_cls, patches["pop_env"]:

            _setup_session_factory(mock_factory)
            _setup_sdk_mock(mock_sdk_cls, text_output=None, cost=0.001)

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
             patches["build_prompt"], \
             patches["sdk_client_cls"] as mock_sdk_cls, patches["pop_env"]:

            _setup_session_factory(mock_factory)

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
             patches["build_prompt"], \
             patches["sdk_client_cls"] as mock_sdk_cls, patches["pop_env"]:

            _setup_session_factory(mock_factory)
            _setup_sdk_mock(mock_sdk_cls, text_output="Review time!")

            await run_proactive_llm_session(user, "proactive_review", {"due_count": 5})

        mock_release.assert_called_once()

    @pytest.mark.asyncio
    async def test_markdown_converted_to_html(self):
        """Markdown in LLM output is converted to Telegram HTML."""
        user = _make_user()
        patches = _patch_infrastructure()

        with patches["pool_acquire"], patches["pool_release"], \
             patches["lock_acquire"], patches["lock_release"], \
             patches["session_factory"] as mock_factory, \
             patches["session_repo_create"], patches["session_repo_update"], \
             patches["build_prompt"], \
             patches["sdk_client_cls"] as mock_sdk_cls, patches["pop_env"]:

            _setup_session_factory(mock_factory)
            _setup_sdk_mock(mock_sdk_cls, text_output="Hello **Alex**!", cost=0.001)

            message, cost = await run_proactive_llm_session(
                user, "proactive_nudge", {},
            )

        assert message == "Hello <b>Alex</b>!"
        assert cost == 0.001

    @pytest.mark.asyncio
    async def test_long_message_truncated(self):
        """Messages exceeding notification_max_length are truncated."""
        user = _make_user()
        patches = _patch_infrastructure()

        with patches["pool_acquire"], patches["pool_release"], \
             patches["lock_acquire"], patches["lock_release"], \
             patches["session_factory"] as mock_factory, \
             patches["session_repo_create"], patches["session_repo_update"], \
             patches["build_prompt"], \
             patches["sdk_client_cls"] as mock_sdk_cls, patches["pop_env"]:

            _setup_session_factory(mock_factory)
            long_text = "A" * 3000  # exceeds 2000 char limit
            _setup_sdk_mock(mock_sdk_cls, text_output=long_text)

            message, cost = await run_proactive_llm_session(
                user, "proactive_nudge", {},
            )

        assert message is not None
        assert message.endswith("...")
        assert len(message) == 2000  # tuning.notification_max_length

    @pytest.mark.asyncio
    async def test_whitespace_only_response_returns_none(self):
        """When LLM outputs only whitespace, returns (None, cost)."""
        user = _make_user()
        patches = _patch_infrastructure()

        with patches["pool_acquire"], patches["pool_release"], \
             patches["lock_acquire"], patches["lock_release"], \
             patches["session_factory"] as mock_factory, \
             patches["session_repo_create"], patches["session_repo_update"], \
             patches["build_prompt"], \
             patches["sdk_client_cls"] as mock_sdk_cls, patches["pop_env"]:

            _setup_session_factory(mock_factory)
            _setup_sdk_mock(mock_sdk_cls, text_output="   \n  ", cost=0.001)

            message, cost = await run_proactive_llm_session(
                user, "proactive_nudge", {},
            )

        assert message is None
