"""Internationalization (i18n) module.

Loads JSON locale files and provides a ``t(key, lang, **kwargs)`` function
that returns localized strings with variable substitution.
"""

import json
import random
import string
from functools import lru_cache
from pathlib import Path

LOCALES_DIR = Path(__file__).parent / "locales"
SUPPORTED_NATIVE_LANGUAGES = frozenset({"en", "ru", "es", "fr", "de", "pt", "it"})
DEFAULT_LANGUAGE = "en"


@lru_cache(maxsize=8)
def _load_locale(lang: str) -> dict[str, str | list[str]]:
    """Load and cache a locale JSON file. Falls back to English."""
    path = LOCALES_DIR / f"{lang}.json"
    if not path.exists():
        path = LOCALES_DIR / f"{DEFAULT_LANGUAGE}.json"
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _extract_format_keys(template: str) -> list[str]:
    """Extract ``{variable}`` names from a format string."""
    formatter = string.Formatter()
    return [
        field_name
        for _, field_name, _, _ in formatter.parse(template)
        if field_name is not None
    ]


def t(key: str, lang: str, **kwargs: object) -> str:
    """Translate a key into the given language with variable substitution.

    Fallback chain: requested lang -> English -> raw key.
    List values (notification template variants) get a random pick.
    """
    if lang not in SUPPORTED_NATIVE_LANGUAGES:
        lang = DEFAULT_LANGUAGE

    locale = _load_locale(lang)
    value = locale.get(key)

    # Fallback to English
    if value is None and lang != DEFAULT_LANGUAGE:
        locale = _load_locale(DEFAULT_LANGUAGE)
        value = locale.get(key)

    if value is None:
        return key  # Return raw key as last resort

    # For list values (notification template variants), pick a random one
    if isinstance(value, list):
        if not value:
            return key
        shuffled = random.sample(value, len(value))
        for variant in shuffled:
            try:
                return variant.format(**kwargs)
            except KeyError:
                continue
        # All variants failed — return first with available kwargs only
        return shuffled[0].format_map(
            {k: kwargs.get(k, "") for k in _extract_format_keys(shuffled[0])},
        )

    try:
        return value.format(**kwargs)
    except KeyError:
        # Substitute available kwargs, use empty string for missing ones
        return value.format_map(
            {k: kwargs.get(k, "") for k in _extract_format_keys(value)},
        )


def get_localized_language_name(code: str, lang: str) -> str:
    """Get a language name localized into the user's native language."""
    return t(f"lang.{code}", lang)


# Known onboarding codes for interests and goals.
# Used to distinguish codes (from onboarding) from free-text (from agent).
KNOWN_INTEREST_CODES = frozenset({
    "food", "music", "sports", "tech", "travel", "news",
    "science", "history", "business", "art", "gaming", "health",
})
KNOWN_GOAL_CODES = frozenset({
    "conversation", "professional", "writing", "exams",
    "travel", "work", "hobby",  # old codes kept for existing users
})


def render_interest(code: str, lang: str = "en") -> str:
    """Render an interest code to a localized label.

    If the code is a known onboarding interest code (e.g. "food"),
    returns the localized label (e.g. "Food & Cooking").
    Otherwise returns the string as-is (free-text from agent).
    """
    if code in KNOWN_INTEREST_CODES:
        return t(f"start.interest_{code}", lang)
    return code


def render_goal(code: str, lang: str = "en", target_language: str = "") -> str:
    """Render a goal code to a localized label.

    If the code is a known onboarding goal code (e.g. "travel"),
    returns the localized label (e.g. "French for travel and conversation").
    Otherwise returns the string as-is (free-text from agent).
    """
    if code in KNOWN_GOAL_CODES:
        return t(f"start.goal_text_{code}", lang, target_language=target_language)
    return code


def reload_locales() -> None:
    """Clear the locale cache (useful for development/testing)."""
    _load_locale.cache_clear()
