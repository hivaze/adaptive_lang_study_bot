"""UX tests for interactive sessions — greeting, comeback, style, difficulty, level.

These tests verify that the agent's user-facing behavior adapts correctly
to user state (session gap, style preference, difficulty, CEFR level,
milestones, notification context, weak areas).  Each test runs a real
Claude Haiku 4.5 session with production system prompt logic.
"""

import re
from datetime import datetime, timedelta, timezone

import pytest

from adaptive_lang_study_bot.agent.prompt_builder import (
    build_system_prompt,
    compute_session_context,
)

pytestmark = [pytest.mark.llm, pytest.mark.timeout(90)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_prompt_for_user(user, *, due_count: int = 0, stale_topics=None):
    """Build production system prompt for a user with custom due_count."""
    ctx = compute_session_context(user)
    return build_system_prompt(user, ctx, due_count=due_count, stale_topics=stale_topics)


# ---------------------------------------------------------------------------
# Greeting & comeback UX
# ---------------------------------------------------------------------------

class TestGreetingAndComeback:

    async def test_long_absence_no_guilt(self, create_llm_session):
        """Returning after 5+ days — agent should be warm, no guilt language."""
        session = await create_llm_session(
            user_overrides={
                "first_name": "Maria",
                "last_session_at": datetime.now(timezone.utc) - timedelta(days=6),
                "streak_days": 0,
                "sessions_completed": 15,
                "recent_scores": [7, 6, 8],
            },
        )
        await session.query_and_collect("Hi, I'm back!")

        lower = session.response_text.lower()
        # Should NOT use guilt-inducing phrases
        guilt_phrases = [
            "you haven't been",
            "you've been away for too long",
            "you missed",
            "you should have",
            "disappointed",
            "falling behind",
            "you need to catch up",
        ]
        for phrase in guilt_phrases:
            assert phrase not in lower, (
                f"Expected no guilt language, but found '{phrase}' in: "
                f"{session.response_text[:300]}"
            )

        # Should contain warm/welcoming tone
        warm_keywords = [
            "welcome", "back", "glad", "happy", "great", "good to see",
            "nice", "hello", "hey", "hi", "return", "missed you",
        ]
        assert any(kw in lower for kw in warm_keywords), (
            f"Expected warm welcome, got: {session.response_text[:300]}"
        )

    async def test_comeback_with_due_vocab_suggests_review(self, create_llm_session):
        """Returning after days with overdue vocab — should suggest review first."""
        user_overrides = {
            "first_name": "Carlos",
            "last_session_at": datetime.now(timezone.utc) - timedelta(days=5),
            "streak_days": 0,
            "sessions_completed": 20,
            "vocabulary_count": 50,
            "recent_scores": [6, 7, 5, 8, 6],
        }
        # Build prompt with due_count > 0 to trigger comeback review priority
        from adaptive_lang_study_bot.db.models import User
        temp_user = User(
            telegram_id=999999,
            **{k: v for k, v in user_overrides.items()},
            native_language="en",
            target_language="es",
            level="A2",
            onboarding_completed=True,
        )
        prompt = _build_prompt_for_user(temp_user, due_count=15)

        session = await create_llm_session(
            user_overrides=user_overrides,
            system_prompt_override=prompt,
        )
        await session.query_and_collect("Hey, I want to practice!")

        lower = session.response_text.lower()
        # Should mention vocabulary review or due cards
        review_keywords = [
            "review", "vocabular", "words", "cards", "overdue",
            "refresh", "recall", "remember", "practiced",
        ]
        assert any(kw in lower for kw in review_keywords), (
            f"Expected comeback to suggest vocab review with 15 due cards, "
            f"got: {session.response_text[:400]}"
        )

    async def test_continuation_minimal_greeting(self, create_llm_session):
        """Returning within 30 min — no elaborate greeting, dive straight in."""
        session = await create_llm_session(
            user_overrides={
                "last_session_at": datetime.now(timezone.utc) - timedelta(minutes=20),
                "sessions_completed": 30,
                "last_activity": {
                    "session_summary": "Practiced food vocabulary",
                    "topic": "food",
                    "score": 8,
                },
            },
        )
        await session.query_and_collect("Let's continue with more food words.")

        # Response should be focused on content, not a long greeting
        # Heuristic: first sentence shouldn't be a standalone greeting paragraph
        # (short responses = continuation, not a brand-new elaborate welcome)
        lower = session.response_text.lower()
        elaborate_greetings = [
            "welcome back",
            "good to see you again",
            "glad you're back",
            "it's been",
        ]
        assert not any(g in lower for g in elaborate_greetings), (
            f"Expected minimal greeting for 20-min gap, but got elaborate greeting: "
            f"{session.response_text[:200]}"
        )


# ---------------------------------------------------------------------------
# Session style UX
# ---------------------------------------------------------------------------

class TestSessionStyleUX:

    async def test_casual_style_relaxed_tone(self, create_llm_session):
        """Casual style should produce a relaxed, conversational response."""
        session = await create_llm_session(
            user_overrides={
                "session_style": "casual",
                "interests": ["music", "movies"],
                "level": "B1",
                "last_session_at": datetime.now(timezone.utc) - timedelta(hours=2),
            },
        )
        await session.query_and_collect(
            "Teach me some useful Spanish expressions for talking about music."
        )

        lower = session.response_text.lower()
        # Casual style should NOT use rigid numbered exercise structure
        # (occasional numbers are fine, but shouldn't read like a textbook)
        rigid_patterns = [
            "exercise 1:", "exercise 2:", "exercise 3:",
            "section 1:", "section 2:",
            "warm-up phase", "main phase", "review phase",
        ]
        rigid_count = sum(1 for p in rigid_patterns if p in lower)
        assert rigid_count == 0, (
            f"Casual style should not use rigid structure markers, "
            f"found {rigid_count}: {session.response_text[:400]}"
        )

    async def test_structured_style_organized(self, create_llm_session):
        """Structured style should produce organized content with clear segments."""
        session = await create_llm_session(
            user_overrides={
                "session_style": "structured",
                "level": "A2",
                "last_session_at": datetime.now(timezone.utc) - timedelta(hours=2),
            },
        )
        await session.query_and_collect(
            "I want to practice past tense in Spanish. "
            "Give me a full exercise session."
        )

        # Structured style should show clear organization:
        # numbered items, bold sections, or explicit segmentation
        has_numbers = bool(re.search(r"\b[1-3]\.", session.response_text))
        has_bold = "<b>" in session.response_text.lower()
        has_structure_words = any(
            w in session.response_text.lower()
            for w in ["exercise", "example", "practice", "translate", "answer"]
        )
        structure_signals = sum([has_numbers, has_bold, has_structure_words])
        assert structure_signals >= 2, (
            f"Structured style should show clear organization "
            f"(numbers={has_numbers}, bold={has_bold}, keywords={has_structure_words}), "
            f"got: {session.response_text[:400]}"
        )

    async def test_intensive_style_dense(self, create_llm_session):
        """Intensive style should be exercise-heavy with minimal preamble."""
        session = await create_llm_session(
            user_overrides={
                "session_style": "intensive",
                "level": "B1",
                "recent_scores": [7, 8, 7, 8, 7],
                # Recent session — avoid comeback greeting overwhelming the style
                "last_session_at": datetime.now(timezone.utc) - timedelta(hours=2),
            },
        )
        await session.query_and_collect(
            "Let's start. Give me exercises on verb conjugation."
        )

        lower = session.response_text.lower()
        # Intensive should jump into exercises quickly
        # Check for exercise content (questions, fill-in, translate, etc.)
        exercise_markers = [
            "translate", "fill", "choose", "complete", "answer",
            "what is", "how do you", "conjugat", "traduc",
            "verb", "tense", "form", "sentence",
        ]
        exercise_signal_count = sum(1 for m in exercise_markers if m in lower)
        # Also count question marks as exercise signals
        question_count = session.response_text.count("?")
        total_exercise_signals = exercise_signal_count + min(question_count, 3)
        assert total_exercise_signals >= 2, (
            f"Intensive style should jump into exercises quickly, "
            f"found only {total_exercise_signals} exercise signals: "
            f"{session.response_text[:400]}"
        )


# ---------------------------------------------------------------------------
# Difficulty UX
# ---------------------------------------------------------------------------

class TestDifficultyUX:

    async def test_easy_provides_scaffolding(self, create_llm_session):
        """Easy difficulty should provide hints, simpler vocab, scaffolded exercises."""
        session = await create_llm_session(
            user_overrides={
                "preferred_difficulty": "easy",
                "level": "A2",
                "recent_scores": [4, 5, 3, 4, 5],
            },
        )
        await session.query_and_collect(
            "Give me a vocabulary exercise about colors."
        )

        lower = session.response_text.lower()
        # Easy exercises should have scaffolding signals:
        # hints, options, choices, or simple prompts
        scaffolding = [
            "hint", "choose", "option", "select", "match",
            "a)", "b)", "c)", "a.", "b.", "c.",
            "multiple", "help", "example",
        ]
        has_scaffolding = any(s in lower for s in scaffolding)
        # Also acceptable: very simple, short exercise prompts
        is_short_simple = len(session.response_text) < 1500
        assert has_scaffolding or is_short_simple, (
            f"Easy difficulty should provide scaffolding or simple exercises, "
            f"got: {session.response_text[:400]}"
        )

    async def test_hard_challenges_student(self, create_llm_session):
        """Hard difficulty should use advanced vocab, complex structures."""
        session = await create_llm_session(
            user_overrides={
                "preferred_difficulty": "hard",
                "level": "B2",
                "recent_scores": [8, 9, 8, 7, 8],
            },
        )
        await session.query_and_collect(
            "Give me a challenging translation exercise."
        )

        lower = session.response_text.lower()
        # Hard exercises should show challenge signals:
        # complex sentences, advanced vocab, or demanding formats
        challenge_signals = [
            "translate", "complex", "advanced", "idiom",
            "nuance", "express", "paragraph", "context",
            "subjunctive", "conditional",
        ]
        # Also check: the exercise text itself is substantial
        has_challenge = any(s in lower for s in challenge_signals)
        is_substantial = len(session.response_text) > 200
        assert has_challenge or is_substantial, (
            f"Hard difficulty should present a challenging exercise, "
            f"got: {session.response_text[:400]}"
        )


# ---------------------------------------------------------------------------
# Level-appropriate content
# ---------------------------------------------------------------------------

class TestLevelAppropriateContent:

    async def test_a1_beginner_simple_content(self, create_llm_session):
        """A1 beginner should get very basic vocabulary and simple exercises."""
        session = await create_llm_session(
            user_overrides={
                "level": "A1",
                "vocabulary_count": 5,
                "sessions_completed": 2,
                "recent_scores": [],
            },
        )
        await session.query_and_collect(
            "Teach me some basic Spanish words."
        )

        lower = session.response_text.lower()
        # A1 content should include basic category words
        basic_categories = [
            "hello", "hola", "thank", "gracia", "please", "por favor",
            "good", "buen", "yes", "sí", "no", "name", "nombre",
            "water", "agua", "food", "comida", "number", "color",
            "house", "casa", "family", "familia",
        ]
        has_basic = any(w in lower for w in basic_categories)
        # Also: no C-level grammar terms
        advanced_terms = ["subjunctive", "pluperfect", "conditional perfect",
                         "discourse marker", "register variation"]
        has_advanced = any(t in lower for t in advanced_terms)
        assert has_basic, (
            f"A1 content should include basic vocabulary, "
            f"got: {session.response_text[:300]}"
        )
        assert not has_advanced, (
            f"A1 content should NOT include advanced grammar terms, "
            f"found in: {session.response_text[:300]}"
        )

    async def test_c2_mastery_sophisticated_content(self, create_llm_session):
        """C2 mastery should get advanced, nuanced content."""
        session = await create_llm_session(
            user_overrides={
                "level": "C2",
                "vocabulary_count": 800,
                "sessions_completed": 100,
                "recent_scores": [9, 8, 9, 10, 9],
                "interests": ["literature", "philosophy"],
            },
        )
        await session.query_and_collect(
            "Give me an advanced exercise that challenges my Spanish."
        )

        lower = session.response_text.lower()
        # C2 should show sophistication: complex sentences, nuance,
        # literary/abstract content, or demanding exercise format
        sophistication_markers = [
            "nuance", "subtle", "literary", "idiomatic", "colloqui",
            "register", "style", "essay", "argumen", "analyz",
            "creative", "paraphras", "interpreta", "express",
            "wordplay", "cultur", "metaphor",
        ]
        has_sophistication = any(m in lower for m in sophistication_markers)
        # Also acceptable: the exercise itself is clearly complex
        # (long sentences in Spanish, advanced vocabulary)
        has_long_spanish = bool(re.search(
            r"[áéíóúñ¿¡].*[áéíóúñ¿¡].*[áéíóúñ¿¡]", session.response_text
        ))
        assert has_sophistication or has_long_spanish, (
            f"C2 content should show advanced sophistication, "
            f"got: {session.response_text[:400]}"
        )


# ---------------------------------------------------------------------------
# Milestone celebrations
# ---------------------------------------------------------------------------

class TestMilestoneCelebrations:

    async def test_pending_celebration_acknowledged(self, create_llm_session):
        """Agent should mention pending milestone celebrations."""
        session = await create_llm_session(
            user_overrides={
                "milestones": {
                    "pending_celebrations": [
                        "7-day streak! Amazing consistency!",
                        "50 words learned — impressive vocabulary!",
                    ],
                },
                "streak_days": 7,
                "vocabulary_count": 50,
            },
        )
        await session.query_and_collect("Hi! Let's study.")

        lower = session.response_text.lower()
        # Agent should acknowledge at least one celebration
        celebration_signals = [
            "streak", "7 day", "seven day", "7-day",
            "50 word", "fifty word", "vocabular",
            "congratul", "amazing", "impressive", "great job",
            "well done", "milestone", "achievement", "celebrate",
        ]
        assert any(s in lower for s in celebration_signals), (
            f"Expected celebration acknowledgment for 7-day streak/50 words, "
            f"got: {session.response_text[:400]}"
        )


# ---------------------------------------------------------------------------
# Notification reply context
# ---------------------------------------------------------------------------

class TestNotificationReplyContext:

    async def test_acknowledges_notification_context(self, create_llm_session):
        """When user replies to a notification, agent should acknowledge it."""
        session = await create_llm_session(
            user_overrides={
                "last_notification_text": "You have 5 vocabulary cards due for review! Start a quick review session?",
                "last_notification_at": datetime.now(timezone.utc) - timedelta(minutes=30),
            },
        )
        await session.query_and_collect("Sure, let's review them!")

        lower = session.response_text.lower()
        # Agent should connect to the notification context
        context_signals = [
            "review", "vocabular", "word", "card",
            "let's", "start", "ready", "great",
        ]
        assert any(s in lower for s in context_signals), (
            f"Expected agent to acknowledge notification context about vocab review, "
            f"got: {session.response_text[:300]}"
        )
        # Should NOT re-send the notification text verbatim
        assert "you have 5 vocabulary cards due" not in lower, (
            "Agent should not repeat the notification verbatim"
        )


# ---------------------------------------------------------------------------
# Weak area focus
# ---------------------------------------------------------------------------

class TestWeakAreaFocus:

    async def test_weak_area_influences_exercise_topic(self, create_llm_session):
        """User with weak areas — agent should offer exercises on those areas."""
        session = await create_llm_session(
            user_overrides={
                "weak_areas": ["verb conjugation", "past tense"],
                "level": "A2",
                "recent_scores": [5, 4, 6, 5, 4],
            },
        )
        await session.query_and_collect(
            "Give me a practice exercise."
        )

        lower = session.response_text.lower()
        # Agent should focus on weak areas (verb conjugation or past tense)
        weak_area_signals = [
            "verb", "conjug", "past", "preterit", "pretérit",
            "imperfect", "tense", "form", "irregular",
        ]
        assert any(s in lower for s in weak_area_signals), (
            f"Expected exercise on weak area (verb conjugation/past tense), "
            f"got: {session.response_text[:400]}"
        )


# ---------------------------------------------------------------------------
# Native language communication
# ---------------------------------------------------------------------------

class TestNativeLanguageCommunication:

    async def test_french_native_gets_french_explanations(self, create_llm_session):
        """French-speaking user should receive explanations in French."""
        session = await create_llm_session(
            user_overrides={
                "native_language": "fr",
                "target_language": "es",
                "first_name": "Pierre",
            },
        )
        await session.query_and_collect(
            "Apprenez-moi quelques mots espagnols."  # "Teach me some Spanish words"
        )

        # Should contain French characters/words in the response
        french_indicators = [
            "voici", "voilà", "les", "des", "une", "est",
            "mot", "espagnol", "signifie", "exemple",
            "exercice", "apprend", "pratiqu",
        ]
        lower = session.response_text.lower()
        french_count = sum(1 for w in french_indicators if w in lower)
        assert french_count >= 2, (
            f"Expected French explanations for fr-native user, "
            f"found only {french_count} French indicators: "
            f"{session.response_text[:300]}"
        )
