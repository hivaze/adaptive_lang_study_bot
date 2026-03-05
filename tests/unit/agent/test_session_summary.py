"""Tests for AI session summary generation — hooks enrichment, prompt building, template fallback."""

import json
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adaptive_lang_study_bot.agent.hooks import SessionHookState, build_session_hooks
from adaptive_lang_study_bot.agent.session_manager import (
    _build_conversation_digest,
    _build_summary_cta_keyboard,
    _build_template_summary,
    _collect_session_data,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_exercise_tool_output(score: int) -> dict:
    return {
        "content": [
            {"type": "text", "text": json.dumps({"score": score, "status": "recorded"})},
        ],
    }


def _make_managed(
    *,
    tools_called: list[str] | None = None,
    hook_state: SessionHookState | None = None,
    native_language: str = "en",
    started_at: float = 0.0,
    turn_count: int = 5,
) -> MagicMock:
    """Build a minimal ManagedSession mock for summary tests."""
    managed = MagicMock()
    managed.tools_called = tools_called or []
    managed.hook_state = hook_state
    managed.native_language = native_language
    managed.started_at = started_at
    managed.turn_count = turn_count
    managed.target_language = "fr"
    managed.first_name = "Test"
    managed.user_level = "B1"
    managed.user_streak = 5
    return managed


# ---------------------------------------------------------------------------
# Hook state enrichment
# ---------------------------------------------------------------------------


class TestHookStateEnrichment:
    """PostToolUse hook captures exercise topics, types, words for summary enrichment."""

    @pytest.fixture()
    def _hook(self):
        hooks, state = build_session_hooks(user_id=1)
        handler = hooks["PostToolUse"][0].hooks[0]
        return handler, state

    @pytest.mark.asyncio
    async def test_tracks_exercise_topic_and_type(self, _hook):
        handler, state = _hook
        await handler(
            {
                "tool_name": "mcp__langbot__record_exercise_result",
                "tool_input": {
                    "exercise_type": "translation",
                    "topic": "food vocabulary",
                    "score": 7,
                },
                "tool_response": _make_exercise_tool_output(7),
            },
            "id-1",
            None,
        )
        assert state.exercise_topics == ["food vocabulary"]
        assert state.exercise_types == ["translation"]

    @pytest.mark.asyncio
    async def test_tracks_words_added(self, _hook):
        handler, state = _hook
        await handler(
            {
                "tool_name": "mcp__langbot__add_vocabulary",
                "tool_input": {"word": "bonjour"},
                "tool_response": "",
            },
            "id-1",
            None,
        )
        assert state.words_added == ["bonjour"]

    @pytest.mark.asyncio
    async def test_tracks_words_reviewed_via_exercise(self, _hook):
        handler, state = _hook
        await handler(
            {
                "tool_name": "mcp__langbot__record_exercise_result",
                "tool_input": {
                    "exercise_type": "translation",
                    "topic": "food",
                    "score": 7,
                },
                "tool_response": {
                    "content": [
                        {"type": "text", "text": json.dumps({
                            "score": 7, "status": "recorded",
                            "vocabulary_reviewed": ["pomme", "fromage"],
                        })},
                    ],
                },
            },
            "id-1",
            None,
        )
        assert state.words_reviewed == 2

    @pytest.mark.asyncio
    async def test_empty_topic_not_tracked(self, _hook):
        handler, state = _hook
        await handler(
            {
                "tool_name": "mcp__langbot__record_exercise_result",
                "tool_input": {"exercise_type": "", "topic": "  ", "score": 5},
                "tool_response": _make_exercise_tool_output(5),
            },
            "id-1",
            None,
        )
        assert state.exercise_topics == []
        assert state.exercise_types == []

    @pytest.mark.asyncio
    async def test_multiple_exercises_accumulate(self, _hook):
        handler, state = _hook
        for topic in ["verbs", "nouns", "verbs"]:
            await handler(
                {
                    "tool_name": "mcp__langbot__record_exercise_result",
                    "tool_input": {
                        "exercise_type": "fill_blank",
                        "topic": topic,
                        "score": 6,
                    },
                    "tool_response": _make_exercise_tool_output(6),
                },
                "id-1",
                None,
            )
        assert state.exercise_topics == ["verbs", "nouns", "verbs"]
        assert len(state.exercise_scores) == 3


# ---------------------------------------------------------------------------
# Template summary
# ---------------------------------------------------------------------------


class TestBuildTemplateSummary:
    def test_exercises_with_topics_and_scores(self):
        state = SessionHookState(user_id=1)
        state.exercise_scores = [7, 8, 9]
        state.exercise_topics = ["verbs", "nouns", "verbs"]

        managed = _make_managed(
            tools_called=[
                "mcp__langbot__record_exercise_result",
                "mcp__langbot__record_exercise_result",
                "mcp__langbot__record_exercise_result",
            ],
            hook_state=state,
        )
        summary = _build_template_summary(managed)
        assert "3" in summary  # exercise count
        assert "verbs" in summary
        assert "nouns" in summary
        # Numeric scores should not appear in user-facing summaries
        assert "8.0" not in summary

    def test_vocab_with_word_samples(self):
        state = SessionHookState(user_id=1)
        state.words_added = ["bonjour", "merci", "salut"]

        managed = _make_managed(
            tools_called=[
                "mcp__langbot__add_vocabulary",
                "mcp__langbot__add_vocabulary",
                "mcp__langbot__add_vocabulary",
            ],
            hook_state=state,
        )
        summary = _build_template_summary(managed)
        assert "3" in summary  # vocab count
        assert "bonjour" in summary
        assert "merci" in summary

    def test_no_progress_uses_no_progress_key(self):
        state = SessionHookState(user_id=1)
        managed = _make_managed(tools_called=[], hook_state=state)
        summary = _build_template_summary(managed)
        # Should NOT contain the old "Practice session completed"
        assert "Practice session completed" not in summary
        # Should contain /words suggestion
        assert "/words" in summary

    def test_reviews_shown(self):
        state = SessionHookState(user_id=1)
        state.words_reviewed = 2
        managed = _make_managed(
            tools_called=[
                "mcp__langbot__record_exercise_result",
                "mcp__langbot__record_exercise_result",
            ],
            hook_state=state,
        )
        summary = _build_template_summary(managed)
        assert "2" in summary  # reviewed word count


# ---------------------------------------------------------------------------
# Collect session data
# ---------------------------------------------------------------------------


class TestCollectSessionData:
    def test_extracts_all_fields(self):
        state = SessionHookState(user_id=1)
        state.exercise_scores = [7, 8]
        state.exercise_topics = ["verbs"]
        state.exercise_types = ["translation"]
        state.words_added = ["bonjour"]
        state.words_reviewed = 2

        managed = _make_managed(
            tools_called=[
                "mcp__langbot__record_exercise_result",
                "mcp__langbot__record_exercise_result",
                "mcp__langbot__add_vocabulary",
            ],
            hook_state=state,
            turn_count=10,
        )
        data = _collect_session_data(managed)
        assert data["exercise_count"] == 2
        assert data["exercise_scores"] == [7, 8]
        assert data["exercise_topics"] == ["verbs"]
        assert data["words_added"] == ["bonjour"]
        assert data["vocab_count"] == 1
        assert data["words_reviewed"] == 2
        assert data["turn_count"] == 10

    def test_no_hook_state_graceful(self):
        managed = _make_managed(tools_called=[], hook_state=None)
        data = _collect_session_data(managed)
        assert data["exercise_count"] == 0
        assert data["exercise_scores"] == []
        assert data["words_added"] == []

    def test_seeded_scores_excluded_from_count(self):
        """Seeded scores (from prior sessions for adaptive hints) should NOT
        inflate the session's exercise count or appear in session_data."""
        state = SessionHookState(user_id=1)
        # Simulate seeding with 3 prior scores
        state.exercise_scores = [8, 9, 10]
        state._seeded_count = 3
        # Then 1 new exercise scored during this session
        state.exercise_scores.append(7)
        state.exercise_topics = ["grammar"]
        state.exercise_types = ["fill-in-the-blank"]

        managed = _make_managed(
            tools_called=["mcp__langbot__record_exercise_result"],
            hook_state=state,
            turn_count=5,
        )
        data = _collect_session_data(managed)
        assert data["exercise_count"] == 1
        assert data["exercise_scores"] == [7]

    def test_seeded_only_no_new_exercises(self):
        """When only seeded scores exist and no exercises were done, count is 0."""
        state = SessionHookState(user_id=1)
        state.exercise_scores = [8, 9]
        state._seeded_count = 2

        managed = _make_managed(
            tools_called=[],
            hook_state=state,
            turn_count=3,
        )
        data = _collect_session_data(managed)
        assert data["exercise_count"] == 0
        assert data["exercise_scores"] == []


# ---------------------------------------------------------------------------
# CTA keyboard
# ---------------------------------------------------------------------------


class TestSummaryCTAKeyboard:
    def test_has_two_buttons(self):
        kb = _build_summary_cta_keyboard("en")
        buttons = kb.inline_keyboard
        assert len(buttons) == 2
        assert buttons[0][0].callback_data == "cta:session"
        assert buttons[1][0].callback_data == "cta:words"


# ---------------------------------------------------------------------------
# Conversation digest builder
# ---------------------------------------------------------------------------


class TestBuildConversationDigest:
    def test_empty_log(self):
        assert _build_conversation_digest([]) == ""

    def test_basic_conversation(self):
        log = [
            {"role": "user", "text": "Bonjour"},
            {"role": "assistant", "text": "Salut! Comment ça va?"},
            {"role": "user", "text": "Ça va bien"},
        ]
        digest = _build_conversation_digest(log)
        assert "[User]: Bonjour" in digest
        assert "[Tutor]: Salut!" in digest
        assert "[User]: Ça va bien" in digest

    def test_tool_calls_included(self):
        log = [
            {"role": "user", "text": "Teach me a word"},
            {"role": "tool_call", "text": "search_vocabulary"},
            {"role": "assistant", "text": "Here's a new word!"},
        ]
        digest = _build_conversation_digest(log)
        assert "[Tool]: search_vocabulary" in digest

    def test_long_conversation_fully_preserved(self):
        log = []
        for i in range(20):
            log.append({"role": "user", "text": f"Message {i}"})
            log.append({"role": "assistant", "text": f"Reply {i}"})

        digest = _build_conversation_digest(log)
        # All messages preserved — no truncation
        assert "Message 0" in digest
        assert "Message 10" in digest
        assert "Reply 19" in digest
