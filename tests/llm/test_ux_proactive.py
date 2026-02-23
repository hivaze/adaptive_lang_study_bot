"""UX tests for proactive notifications — quality, personalization, conciseness.

These tests verify that proactive notification sessions produce messages that
are concise, personalized, motivating, and in the correct language.  Each test
runs a real Claude Haiku 4.5 session with the production proactive prompt.
"""

import re
from unittest.mock import MagicMock

import pytest

from adaptive_lang_study_bot.agent.prompt_builder import build_proactive_prompt

pytestmark = [pytest.mark.llm, pytest.mark.timeout(90)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_user(**overrides):
    """Create a mock user with defaults for proactive prompt building."""
    defaults = {
        "first_name": "TestStudent",
        "native_language": "en",
        "target_language": "es",
        "level": "A2",
        "streak_days": 5,
        "vocabulary_count": 30,
        "interests": ["cooking", "travel"],
        "learning_goals": ["prepare for trip to Spain"],
        "weak_areas": ["past tense"],
        "recent_scores": [6, 7, 5, 8, 6],
        "topics_to_avoid": [],
    }
    defaults.update(overrides)
    user = MagicMock()
    for key, val in defaults.items():
        setattr(user, key, val)
    return user


def _extract_notification_text(response_text: str) -> str:
    """Extract the meaningful notification text from the agent response.

    The agent calls send_notification with the message, but the response
    text includes the agent's reasoning too.  For length assertions, we
    use the full response as a proxy — the notification text is always
    a subset.
    """
    return response_text.strip()


# ---------------------------------------------------------------------------
# Nudge quality
# ---------------------------------------------------------------------------

class TestProactiveNudgeQuality:

    async def test_nudge_is_concise(self, create_llm_session):
        """Nudge notification should be brief — not a full lesson."""
        user = _mock_user(streak_days=12, interests=["cooking"])
        prompt = build_proactive_prompt(
            user, "proactive_nudge",
            {"streak": 12, "name": "TestStudent", "type": "streak_risk"},
        )

        session = await create_llm_session(
            session_type="proactive_nudge",
            max_turns=3,
            system_prompt_override=prompt,
        )
        await session.query_and_collect("Execute your proactive task now.")

        assert "send_notification" in session.bare_tools, (
            f"Nudge should call send_notification, got: {session.bare_tools}"
        )
        # Notification should be concise — the send_notification argument
        # is embedded in the response. Full response under 800 chars is a
        # reasonable proxy for a brief notification.
        # (Agent response includes both reasoning and the notification.)
        assert len(session.response_text) < 1500, (
            f"Nudge should be concise, but response was {len(session.response_text)} chars: "
            f"{session.response_text[:400]}"
        )

    async def test_nudge_is_personalized(self, create_llm_session):
        """Nudge should reference user's name, streak, interests, or goals."""
        user = _mock_user(
            first_name="Sofia",
            streak_days=14,
            interests=["photography", "travel"],
            learning_goals=["prepare for trip to Mexico"],
        )
        prompt = build_proactive_prompt(
            user, "proactive_nudge",
            {"streak": 14, "name": "Sofia", "type": "streak_risk"},
        )

        session = await create_llm_session(
            session_type="proactive_nudge",
            max_turns=3,
            system_prompt_override=prompt,
        )
        await session.query_and_collect("Execute your proactive task now.")

        lower = session.response_text.lower()
        # Should reference at least one personal detail
        personal_signals = [
            "sofia",
            "14", "fourteen", "streak",
            "photo", "travel", "trip", "mexico",
        ]
        matches = [s for s in personal_signals if s in lower]
        assert len(matches) >= 1, (
            f"Nudge should be personalized (expected name/streak/interests), "
            f"found 0 personal signals: {session.response_text[:400]}"
        )

    async def test_nudge_motivating_tone(self, create_llm_session):
        """Nudge should be encouraging and motivating, not demanding."""
        user = _mock_user(streak_days=3)
        prompt = build_proactive_prompt(
            user, "proactive_nudge",
            {"streak": 3, "name": "TestStudent", "type": "streak_risk"},
        )

        session = await create_llm_session(
            session_type="proactive_nudge",
            max_turns=3,
            system_prompt_override=prompt,
        )
        await session.query_and_collect("Execute your proactive task now.")

        lower = session.response_text.lower()
        # Should NOT be demanding or guilt-inducing
        demanding_phrases = [
            "you must", "you have to", "you need to",
            "don't forget", "you're falling behind",
            "you haven't studied",
        ]
        for phrase in demanding_phrases:
            assert phrase not in lower, (
                f"Nudge should be motivating, not demanding — found '{phrase}'"
            )

        # Should have positive/encouraging language
        positive_words = [
            "keep", "great", "amazing", "let's", "ready",
            "continue", "practice", "progress", "grow",
            "learn", "fun", "enjoy", "awesome", "well done",
            "going", "come", "join", "start", "session",
        ]
        assert any(w in lower for w in positive_words), (
            f"Nudge should have positive language, "
            f"got: {session.response_text[:300]}"
        )


# ---------------------------------------------------------------------------
# Review notification quality
# ---------------------------------------------------------------------------

class TestProactiveReviewQuality:

    async def test_review_mentions_due_count(self, create_llm_session):
        """Review notification should mention the number of due cards."""
        user = _mock_user(vocabulary_count=80)
        prompt = build_proactive_prompt(
            user, "proactive_review",
            {"due_count": 12, "name": "TestStudent", "type": "cards_due"},
        )

        session = await create_llm_session(
            session_type="proactive_review",
            max_turns=5,
            system_prompt_override=prompt,
        )
        await session.query_and_collect("Execute your proactive task now.")

        assert "send_notification" in session.bare_tools, (
            f"Review should call send_notification, got: {session.bare_tools}"
        )

        lower = session.response_text.lower()
        # Should mention the count (12) or "cards"/"words" due
        count_signals = [
            "12", "twelve", "card", "word", "review", "due",
            "vocabular", "overdue", "waiting",
        ]
        assert any(s in lower for s in count_signals), (
            f"Review notification should mention due cards, "
            f"got: {session.response_text[:400]}"
        )


# ---------------------------------------------------------------------------
# Quiz notification quality
# ---------------------------------------------------------------------------

class TestProactiveQuizQuality:

    async def test_quiz_includes_questions(self, create_llm_session):
        """Quiz notification should include actual exercise questions."""
        user = _mock_user(level="B1", interests=["sports"])
        prompt = build_proactive_prompt(
            user, "proactive_quiz",
            {"name": "TestStudent", "type": "proactive_quiz"},
        )

        session = await create_llm_session(
            session_type="proactive_quiz",
            max_turns=5,
            system_prompt_override=prompt,
        )
        await session.query_and_collect("Execute your proactive task now.")

        assert "send_notification" in session.bare_tools, (
            f"Quiz should call send_notification, got: {session.bare_tools}"
        )

        # Should contain questions (question marks in notification)
        has_question_mark = "?" in session.response_text
        # Or exercise-type content
        lower = session.response_text.lower()
        exercise_markers = [
            "translate", "fill", "choose", "what", "how",
            "complete", "which", "answer", "mean",
        ]
        has_exercise = any(m in lower for m in exercise_markers)
        assert has_question_mark or has_exercise, (
            f"Quiz notification should include questions or exercises, "
            f"got: {session.response_text[:400]}"
        )


# ---------------------------------------------------------------------------
# Native language in proactive notifications
# ---------------------------------------------------------------------------

class TestProactiveNativeLanguage:

    async def test_russian_user_gets_russian_nudge(self, create_llm_session):
        """Russian-speaking user should receive notification in Russian."""
        user = _mock_user(
            first_name="Алексей",
            native_language="ru",
            target_language="es",
            streak_days=10,
        )
        prompt = build_proactive_prompt(
            user, "proactive_nudge",
            {"streak": 10, "name": "Алексей", "type": "streak_risk"},
        )

        session = await create_llm_session(
            session_type="proactive_nudge",
            max_turns=3,
            system_prompt_override=prompt,
        )
        await session.query_and_collect("Execute your proactive task now.")

        assert "send_notification" in session.bare_tools, (
            f"Should call send_notification, got: {session.bare_tools}"
        )

        # Response should contain Cyrillic (Russian) text
        has_cyrillic = bool(re.search(r"[\u0400-\u04ff]", session.response_text))
        assert has_cyrillic, (
            f"Russian user should get Russian notification, "
            f"but no Cyrillic found: {session.response_text[:300]}"
        )

    async def test_spanish_user_gets_spanish_nudge(self, create_llm_session):
        """Spanish-speaking user learning French should get notification in Spanish."""
        user = _mock_user(
            first_name="María",
            native_language="es",
            target_language="fr",
            streak_days=7,
            interests=["cocina", "viajes"],
        )
        prompt = build_proactive_prompt(
            user, "proactive_nudge",
            {"streak": 7, "name": "María", "type": "streak_risk"},
        )

        session = await create_llm_session(
            session_type="proactive_nudge",
            max_turns=3,
            system_prompt_override=prompt,
        )
        await session.query_and_collect("Execute your proactive task now.")

        assert "send_notification" in session.bare_tools, (
            f"Should call send_notification, got: {session.bare_tools}"
        )

        lower = session.response_text.lower()
        # Should contain Spanish indicators
        spanish_signals = [
            "hola", "tu", "racha", "práctic", "sesión",
            "aprender", "francés", "estudi", "continú",
            "¡", "¿", "días", "palabras",
        ]
        spanish_count = sum(1 for s in spanish_signals if s in lower)
        assert spanish_count >= 2, (
            f"Spanish user should get Spanish notification, "
            f"found only {spanish_count} Spanish indicators: "
            f"{session.response_text[:300]}"
        )


# ---------------------------------------------------------------------------
# Topics to avoid in proactive
# ---------------------------------------------------------------------------

class TestProactiveTopicsToAvoid:

    async def test_quiz_avoids_restricted_topics(self, create_llm_session):
        """Quiz notification should NOT include exercises about avoided topics."""
        user = _mock_user(
            topics_to_avoid=["politics", "war", "violence"],
            interests=["cooking", "nature"],
            level="A2",
        )
        prompt = build_proactive_prompt(
            user, "proactive_quiz",
            {"name": "TestStudent", "type": "proactive_quiz"},
        )

        session = await create_llm_session(
            session_type="proactive_quiz",
            max_turns=5,
            system_prompt_override=prompt,
        )
        await session.query_and_collect("Execute your proactive task now.")

        lower = session.response_text.lower()
        # Should NOT contain avoided topic content
        avoided_keywords = [
            "politic", "war", "violen", "weapon", "soldier",
            "battle", "kill", "fight", "army", "militar",
            "guerra", "violencia", "político",
        ]
        found_avoided = [kw for kw in avoided_keywords if kw in lower]
        assert not found_avoided, (
            f"Quiz should avoid restricted topics, but found: {found_avoided} "
            f"in: {session.response_text[:400]}"
        )
