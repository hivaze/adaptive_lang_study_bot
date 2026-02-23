"""Unit tests for split_agent_sections — the === delimiter splitting logic."""

from adaptive_lang_study_bot.bot.helpers import split_agent_sections


def test_no_delimiter_returns_single_section():
    text = "Hello, let's start learning!"
    assert split_agent_sections(text) == ["Hello, let's start learning!"]


def test_single_split():
    text = "Greeting!\n===\nNow let's do an exercise."
    result = split_agent_sections(text)
    assert result == ["Greeting!", "Now let's do an exercise."]


def test_multiple_splits():
    text = "Part 1\n===\nPart 2\n===\nPart 3"
    result = split_agent_sections(text)
    assert result == ["Part 1", "Part 2", "Part 3"]


def test_whitespace_around_delimiter():
    text = "Part 1\n  ===  \nPart 2"
    result = split_agent_sections(text)
    assert result == ["Part 1", "Part 2"]


def test_empty_sections_filtered():
    text = "\n===\n\n===\nActual content"
    result = split_agent_sections(text)
    assert result == ["Actual content"]


def test_strips_whitespace_from_sections():
    text = "  Hello  \n===\n  World  "
    result = split_agent_sections(text)
    assert result == ["Hello", "World"]


def test_delimiter_not_on_own_line_ignored():
    """=== embedded inline (not on its own line) should NOT split."""
    text = "Score: 5/10 === not bad"
    result = split_agent_sections(text)
    assert result == ["Score: 5/10 === not bad"]


def test_empty_string():
    assert split_agent_sections("") == []


def test_only_delimiter():
    # Just a delimiter with newlines around it — no real content
    assert split_agent_sections("\n===\n") == []


def test_html_preserved():
    text = "<b>Great job!</b>\n===\nNow try: <i>traducir</i>"
    result = split_agent_sections(text)
    assert result == ["<b>Great job!</b>", "Now try: <i>traducir</i>"]
