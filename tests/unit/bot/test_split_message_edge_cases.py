"""Tests for message splitting edge cases (discovered during code audit)."""

from adaptive_lang_study_bot.bot.routers.chat import _split_message


class TestSplitMessageEdgeCases:
    """Regression tests for _split_message edge cases."""

    def test_no_empty_parts_from_consecutive_newlines(self):
        """Empty parts should be filtered out when text has consecutive delimiters."""
        # Construct text that would produce an empty first part after rstrip
        text = "\n\n" + "a" * 5000
        result = _split_message(text, max_len=4096)
        for part in result:
            assert part, "Empty parts must not be produced"

    def test_no_empty_parts_from_whitespace_only_chunk(self):
        """Whitespace-only chunks between delimiters should be skipped."""
        text = "a" * 3000 + "\n\n   \n\n" + "b" * 3000
        result = _split_message(text, max_len=4096)
        for part in result:
            assert part.strip(), "Whitespace-only parts must not be produced"

    def test_all_parts_within_limit(self):
        """Every part must respect the max_len limit."""
        text = "word " * 2000  # ~10000 chars
        result = _split_message(text, max_len=100)
        for part in result:
            assert len(part) <= 100

    def test_no_infinite_loop_on_leading_delimiter(self):
        """Text starting with a delimiter at position 0 must not cause infinite loop."""
        text = " " + "a" * 5000
        result = _split_message(text, max_len=4096)
        assert len(result) >= 1
        total = sum(len(p) for p in result)
        assert total > 0

    def test_returns_nonempty_list_for_all_whitespace(self):
        """Even all-whitespace input should not produce an empty list."""
        text = " " * 5000
        result = _split_message(text, max_len=4096)
        assert len(result) >= 1

    def test_single_long_word_force_split(self):
        """A single word exceeding max_len must be hard-split, not loop forever."""
        text = "a" * 10000
        result = _split_message(text, max_len=4096)
        assert len(result) >= 2
        # Rejoin must equal original
        assert "".join(result) == text
