"""Tests for AI session summary generation — hooks enrichment, prompt building, template fallback."""

import json
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adaptive_lang_study_bot.agent.hooks import SessionHookState, build_session_hooks
from adaptive_lang_study_bot.agent.prompt_builder import build_summary_prompt
from adaptive_lang_study_bot.agent.session_manager import (
    _build_summary_cta_keyboard,
    _build_template_summary,
    _collect_session_data,
)
from adaptive_lang_study_bot.enums import CloseReason


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
    async def test_tracks_words_reviewed(self, _hook):
        handler, state = _hook
        await handler(
            {
                "tool_name": "mcp__langbot__record_vocabulary_review",
                "tool_input": {"vocabulary_id": 42, "rating": 3},
                "tool_response": "",
            },
            "id-1",
            None,
        )
        assert state.words_reviewed == 1

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
# Summary prompt builder
# ---------------------------------------------------------------------------


class TestBuildSummaryPrompt:
    def test_progress_case_mentions_topics(self):
        prompt = build_summary_prompt(
            "ru",
            "en",
            session_data={
                "exercise_count": 3,
                "exercise_scores": [7, 8, 6],
                "exercise_topics": ["verbs", "nouns"],
                "exercise_types": ["translation"],
                "words_added": ["hello"],
                "words_reviewed": 0,
                "vocab_count": 1,
                "review_count": 0,
                "turn_count": 10,
                "duration_minutes": 5,
            },
            close_reason=CloseReason.IDLE_TIMEOUT,
            user_name="Alice",
            user_streak=7,
            user_level="B1",
        )
        assert "verbs" in prompt
        assert "nouns" in prompt
        assert "hello" in prompt
        assert "Summarize" in prompt

    def test_no_progress_case_suggests_exercises(self):
        prompt = build_summary_prompt(
            "en",
            "fr",
            session_data={
                "exercise_count": 0,
                "exercise_scores": [],
                "exercise_topics": [],
                "exercise_types": [],
                "words_added": [],
                "words_reviewed": 0,
                "vocab_count": 0,
                "review_count": 0,
                "turn_count": 3,
                "duration_minutes": 2,
            },
            close_reason=CloseReason.EXPLICIT_CLOSE,
            user_name="Bob",
            user_streak=0,
            user_level="A1",
        )
        assert "exercise" in prompt.lower() or "/words" in prompt
        assert "guilt" not in prompt.lower() or "guilt-trip" in prompt.lower()

    def test_close_reason_shapes_tone(self):
        base_data = {
            "exercise_count": 0,
            "exercise_scores": [],
            "exercise_topics": [],
            "exercise_types": [],
            "words_added": [],
            "words_reviewed": 0,
            "vocab_count": 0,
            "review_count": 0,
            "turn_count": 3,
            "duration_minutes": 2,
        }
        idle_prompt = build_summary_prompt(
            "en", "fr",
            session_data=base_data,
            close_reason=CloseReason.IDLE_TIMEOUT,
            user_name="X", user_streak=0, user_level="A1",
        )
        turn_prompt = build_summary_prompt(
            "en", "fr",
            session_data=base_data,
            close_reason=CloseReason.TURN_LIMIT,
            user_name="X", user_streak=0, user_level="A1",
        )
        assert "stopped responding" in idle_prompt.lower()
        assert "productivity" in turn_prompt.lower()

    def test_no_header_rule_present(self):
        prompt = build_summary_prompt(
            "ru",
            "en",
            session_data={
                "exercise_count": 2,
                "exercise_scores": [7, 8],
                "exercise_topics": ["verbs"],
                "exercise_types": ["translation"],
                "words_added": [],
                "words_reviewed": 0,
                "vocab_count": 0,
                "review_count": 0,
                "turn_count": 5,
                "duration_minutes": 3,
            },
            close_reason=CloseReason.IDLE_TIMEOUT,
            user_name="Сергей",
            user_streak=3,
            user_level="A2",
        )
        assert "NEVER begin with ANY header" in prompt
        assert "jump straight into the message content" in prompt

    def test_native_language_instruction(self):
        prompt = build_summary_prompt(
            "ru",
            "en",
            session_data={
                "exercise_count": 0, "exercise_scores": [],
                "exercise_topics": [], "exercise_types": [],
                "words_added": [], "words_reviewed": 0,
                "vocab_count": 0, "review_count": 0,
                "turn_count": 1, "duration_minutes": 1,
            },
            close_reason=CloseReason.IDLE_TIMEOUT,
            user_name="X", user_streak=0, user_level="A1",
        )
        assert "Russian" in prompt


# ---------------------------------------------------------------------------
# Template summary (enriched fallback)
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
        managed = _make_managed(
            tools_called=[
                "mcp__langbot__record_vocabulary_review",
                "mcp__langbot__record_vocabulary_review",
            ],
            hook_state=state,
        )
        summary = _build_template_summary(managed)
        assert "2" in summary  # review count


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
                "mcp__langbot__record_vocabulary_review",
                "mcp__langbot__record_vocabulary_review",
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
        assert data["review_count"] == 2
        assert data["turn_count"] == 10

    def test_no_hook_state_graceful(self):
        managed = _make_managed(tools_called=[], hook_state=None)
        data = _collect_session_data(managed)
        assert data["exercise_count"] == 0
        assert data["exercise_scores"] == []
        assert data["words_added"] == []


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
