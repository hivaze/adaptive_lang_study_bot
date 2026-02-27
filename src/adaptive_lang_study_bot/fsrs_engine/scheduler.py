from datetime import datetime, timedelta, timezone

from fsrs import Card, Rating, Scheduler
from loguru import logger

from adaptive_lang_study_bot.config import tuning
from adaptive_lang_study_bot.db.models import Vocabulary

_scheduler = Scheduler(
    learning_steps=tuple(timedelta(minutes=m) for m in tuning.fsrs_learning_steps_minutes),
    relearning_steps=tuple(timedelta(minutes=m) for m in tuning.fsrs_relearning_steps_minutes),
)

# Map our rating ints to FSRS Rating enum
_RATING_MAP: dict[int, Rating] = {
    1: Rating.Again,
    2: Rating.Hard,
    3: Rating.Good,
    4: Rating.Easy,
}

# Map FSRS State enum values to human-readable names
_STATE_NAMES: dict[int, str] = {
    0: "New",
    1: "Learning",
    2: "Review",
    3: "Relearning",
}


def _get_state_value(card: Card) -> int:
    """Extract numeric state value, handling FSRS version differences."""
    return card.state.value if hasattr(card.state, "value") else int(card.state)


def create_new_card() -> dict:
    """Create a new FSRS card and return its serialized data."""
    card = Card()
    state_value = _get_state_value(card)
    due = card.due
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return {
        "card_data": card.to_dict(),
        "state": state_value,
        "stability": card.stability,
        "difficulty": card.difficulty,
        "due": due,
    }


def review_card(vocab: Vocabulary, rating: int) -> dict:
    """Review a vocabulary card with the given rating.

    Args:
        vocab: The Vocabulary ORM model with fsrs_data
        rating: 1=Again, 2=Hard, 3=Good, 4=Easy

    Returns:
        Dict with updated FSRS state for DB storage.
    """
    # Reconstruct the card from stored data, resetting on corruption
    if vocab.fsrs_data:
        try:
            card = Card.from_dict(vocab.fsrs_data)
        except (ValueError, KeyError, TypeError):
            logger.warning("Corrupted FSRS data for vocab {}, resetting card", vocab.id)
            card = Card()
    else:
        card = Card()

    fsrs_rating = _RATING_MAP[rating]
    now = datetime.now(timezone.utc)

    # Schedule the review — returns (updated_card, review_log) tuple
    updated_card, review_log = _scheduler.review_card(card, fsrs_rating, now)

    state_value = _get_state_value(updated_card)

    due = updated_card.due
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)

    # Calculate scheduled days from due date (clamp to 0 for immediate reviews)
    scheduled_days = max(0, (due - now).total_seconds() / 86400)

    return {
        "state": state_value,
        "state_name": _STATE_NAMES.get(state_value, "Unknown"),
        "stability": updated_card.stability,
        "difficulty": updated_card.difficulty,
        "due": due,
        "last_review": now,
        "card_data": updated_card.to_dict(),
        "scheduled_days": scheduled_days,
    }


def get_card_state_name(state: int) -> str:
    """Get human-readable state name."""
    return _STATE_NAMES.get(state, "Unknown")
