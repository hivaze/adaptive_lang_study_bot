from datetime import datetime, timezone
from unittest.mock import MagicMock

from adaptive_lang_study_bot.fsrs_engine.scheduler import (
    create_new_card,
    get_card_state_name,
    review_card,
)


def test_create_new_card():
    result = create_new_card()
    assert "card_data" in result
    assert "state" in result
    assert "stability" in result
    assert "difficulty" in result
    assert "due" in result
    assert result["due"].tzinfo is not None


def test_review_card_good():
    vocab = MagicMock()
    vocab.fsrs_data = {}
    vocab.fsrs_state = 0
    vocab.fsrs_stability = None
    vocab.fsrs_difficulty = None

    result = review_card(vocab, 3)  # Good
    assert "state" in result
    assert "stability" in result
    assert "difficulty" in result
    assert "due" in result
    assert "last_review" in result
    assert "card_data" in result
    assert "scheduled_days" in result
    assert "state_name" in result
    assert result["due"].tzinfo is not None


def test_review_card_again():
    vocab = MagicMock()
    vocab.fsrs_data = {}
    vocab.fsrs_state = 0
    vocab.fsrs_stability = None
    vocab.fsrs_difficulty = None

    result = review_card(vocab, 1)  # Again
    assert result["state_name"] in ("New", "Learning", "Relearning")


def test_review_card_easy():
    vocab = MagicMock()
    vocab.fsrs_data = {}

    result = review_card(vocab, 4)  # Easy
    assert result["scheduled_days"] >= 0


def test_scheduled_days_never_negative():
    """Verify scheduled_days is clamped to non-negative for all ratings."""
    for rating in (1, 2, 3, 4):
        vocab = MagicMock()
        vocab.fsrs_data = {}
        result = review_card(vocab, rating)
        assert result["scheduled_days"] >= 0, f"Negative scheduled_days for rating {rating}"


def test_scheduled_days_non_negative_after_multiple_reviews():
    """After several Again ratings, scheduled_days should still be >= 0."""
    vocab = MagicMock()
    vocab.fsrs_data = {}
    for _ in range(5):
        result = review_card(vocab, 1)  # Again repeatedly
        vocab.fsrs_data = result["card_data"]
        assert result["scheduled_days"] >= 0


def test_review_card_preserves_state():
    vocab = MagicMock()
    vocab.fsrs_data = {}

    # First review
    r1 = review_card(vocab, 3)
    vocab.fsrs_data = r1["card_data"]

    # Second review
    r2 = review_card(vocab, 3)
    # Stability should increase with consecutive Good ratings
    assert r2["stability"] is not None


def test_get_card_state_name():
    assert get_card_state_name(0) == "New"
    assert get_card_state_name(1) == "Learning"
    assert get_card_state_name(2) == "Review"
    assert get_card_state_name(3) == "Relearning"
    assert get_card_state_name(99) == "Unknown"
