from adaptive_lang_study_bot.bot.routers.chat import _get_open_tags, _split_message


class TestSplitMessage:

    def test_short_message_not_split(self):
        text = "Hello, world!"
        result = _split_message(text)
        assert result == [text]

    def test_exactly_max_len(self):
        text = "a" * 4096
        result = _split_message(text, max_len=4096)
        assert result == [text]

    def test_splits_on_double_newline(self):
        paragraph1 = "a" * 3000
        paragraph2 = "b" * 3000
        text = paragraph1 + "\n\n" + paragraph2
        result = _split_message(text, max_len=4096)
        assert len(result) == 2
        assert result[0] == paragraph1
        assert result[1] == paragraph2

    def test_splits_on_single_newline(self):
        line1 = "a" * 2500
        line2 = "b" * 2500
        text = line1 + "\n" + line2
        result = _split_message(text, max_len=4096)
        assert len(result) == 2

    def test_splits_on_space(self):
        word1 = "a" * 2500
        word2 = "b" * 2500
        text = word1 + " " + word2
        result = _split_message(text, max_len=4096)
        assert len(result) == 2

    def test_force_split_no_boundaries(self):
        """When there are no split boundaries, split at effective max (with tag reserve)."""
        text = "a" * 8000
        result = _split_message(text, max_len=4096)
        assert len(result) >= 2
        # Each part must fit within max_len
        for part in result:
            assert len(part) <= 4096
        # All content preserved
        assert "".join(result) == text

    def test_empty_string(self):
        result = _split_message("")
        assert result == [""]

    def test_multiple_splits(self):
        text = ("a" * 3000 + "\n\n") * 5
        result = _split_message(text.strip(), max_len=4096)
        assert len(result) >= 2
        for part in result:
            assert len(part) <= 4096

    def test_html_tags_balanced_across_split(self):
        """Unclosed HTML tags are closed at the split and reopened in the next part."""
        inner = "a" * 3000
        text = f"<b>{inner}\n\n{inner}</b>"
        result = _split_message(text, max_len=4096)
        assert len(result) == 2
        # First part should close the <b> tag
        assert result[0].endswith("</b>")
        # Second part should reopen the <b> tag
        assert result[1].startswith("<b>")

    def test_nested_html_tags_balanced(self):
        """Nested tags are properly closed and reopened."""
        inner = "a" * 3000
        text = f"<b><i>{inner}\n\n{inner}</i></b>"
        result = _split_message(text, max_len=4096)
        assert len(result) == 2
        # First part closes in reverse order: </i></b>
        assert result[0].endswith("</i></b>")
        # Second part reopens in original order: <b><i>
        assert result[1].startswith("<b><i>")

    def test_closed_tags_not_duplicated(self):
        """Already-closed tags are not re-closed at split point."""
        part1 = "<b>bold</b> " + "a" * 3000
        part2 = "b" * 3000
        text = part1 + "\n\n" + part2
        result = _split_message(text, max_len=4096)
        assert len(result) == 2
        # No dangling closing tags — <b> was already closed
        assert not result[0].endswith("</b></b>")
        assert not result[1].startswith("<b>")


class TestGetOpenTags:

    def test_no_tags(self):
        assert _get_open_tags("hello world") == []

    def test_closed_tag(self):
        assert _get_open_tags("<b>bold</b>") == []

    def test_unclosed_tag(self):
        assert _get_open_tags("<b>bold") == ["<b>"]

    def test_nested_unclosed(self):
        assert _get_open_tags("<b><i>text") == ["<b>", "<i>"]

    def test_partially_closed(self):
        assert _get_open_tags("<b><i>text</i>") == ["<b>"]

    def test_tag_with_attributes(self):
        tags = _get_open_tags('<a href="http://example.com">link')
        assert len(tags) == 1
        assert 'href="http://example.com"' in tags[0]
