"""Tests for the markdown_to_telegram_html converter."""

import pytest

from adaptive_lang_study_bot.bot.helpers import markdown_to_telegram_html


class TestBold:
    def test_simple_bold(self):
        assert markdown_to_telegram_html("**hello**") == "<b>hello</b>"

    def test_bold_in_sentence(self):
        result = markdown_to_telegram_html("This is **important** text.")
        assert result == "This is <b>important</b> text."

    def test_multiple_bold(self):
        result = markdown_to_telegram_html("**one** and **two**")
        assert result == "<b>one</b> and <b>two</b>"


class TestItalic:
    def test_simple_italic(self):
        assert markdown_to_telegram_html("*hello*") == "<i>hello</i>"

    def test_italic_in_sentence(self):
        result = markdown_to_telegram_html("This is *emphasized* text.")
        assert result == "This is <i>emphasized</i> text."


class TestBoldAndItalic:
    def test_bold_then_italic(self):
        result = markdown_to_telegram_html("**bold** and *italic*")
        assert result == "<b>bold</b> and <i>italic</i>"

    def test_italic_not_consumed_by_bold(self):
        """Single asterisks should not be consumed by the bold regex."""
        result = markdown_to_telegram_html("*italic* not **bold**")
        assert "<i>italic</i>" in result
        assert "<b>bold</b>" in result


class TestInlineCode:
    def test_simple_code(self):
        assert markdown_to_telegram_html("`bonjour`") == "<code>bonjour</code>"

    def test_code_not_processed_inside(self):
        """Bold/italic markers inside inline code should be left alone."""
        result = markdown_to_telegram_html("`**not bold**`")
        assert result == "<code>**not bold**</code>"


class TestCodeBlock:
    def test_code_block(self):
        text = "```python\nprint('hello')\n```"
        result = markdown_to_telegram_html(text)
        assert result == "<pre>print('hello')\n</pre>"

    def test_code_block_no_language(self):
        text = "```\nsome code\n```"
        result = markdown_to_telegram_html(text)
        assert result == "<pre>some code\n</pre>"

    def test_code_block_content_not_processed(self):
        text = "```\n**bold** and *italic*\n```"
        result = markdown_to_telegram_html(text)
        assert "**bold**" in result
        assert "*italic*" in result


class TestHeaders:
    def test_h1(self):
        result = markdown_to_telegram_html("# Title")
        assert result == "<b>Title</b>"

    def test_h2(self):
        result = markdown_to_telegram_html("## Section")
        assert result == "<b>Section</b>"

    def test_h3(self):
        result = markdown_to_telegram_html("### Subsection")
        assert result == "<b>Subsection</b>"

    def test_header_in_text(self):
        result = markdown_to_telegram_html("Intro\n\n## Section\n\nBody")
        assert "<b>Section</b>" in result
        assert "Intro" in result
        assert "Body" in result

    def test_not_header_without_space(self):
        """#tag should not be treated as header."""
        result = markdown_to_telegram_html("#hashtag")
        assert result == "#hashtag"


class TestHorizontalRule:
    def test_hr_removed(self):
        result = markdown_to_telegram_html("above\n---\nbelow")
        assert "---" not in result
        assert "above" in result
        assert "below" in result

    def test_long_hr(self):
        result = markdown_to_telegram_html("text\n-----\nmore")
        assert "-----" not in result


class TestLinks:
    def test_simple_link(self):
        result = markdown_to_telegram_html("[click](https://example.com)")
        assert result == '<a href="https://example.com">click</a>'


class TestPassthrough:
    def test_plain_text_unchanged(self):
        text = "Hello, this is plain text."
        assert markdown_to_telegram_html(text) == text

    def test_existing_html_passed_through(self):
        """Already-HTML text from locale strings should pass through."""
        text = "<b>bold</b> and <i>italic</i>"
        assert markdown_to_telegram_html(text) == text

    def test_numbered_list_unchanged(self):
        text = "1. First\n2. Second\n3. Third"
        assert markdown_to_telegram_html(text) == text

    def test_emoji_unchanged(self):
        text = "Great job! 🎉"
        assert markdown_to_telegram_html(text) == text


class TestMixedContent:
    def test_realistic_agent_output(self):
        text = (
            "**Упражнение: заполните пропуски**\n\n"
            "1. «Je _____ (быть) développeur à Paris.»\n"
            "2. «Je _____ (жить) en France depuis six mois.»\n\n"
            "Напишите ответы вот так:\n"
            "1. suis\n"
            "2. ..."
        )
        result = markdown_to_telegram_html(text)
        assert "<b>Упражнение: заполните пропуски</b>" in result
        assert "1. «Je" in result  # numbered list preserved

    def test_header_with_bold_content(self):
        text = "## Сегмент 1: Административная лексика\n\nsome content"
        result = markdown_to_telegram_html(text)
        assert "<b>Сегмент 1: Административная лексика</b>" in result

    def test_vocabulary_list(self):
        text = (
            "- **la préfecture** (префектура)\n"
            "- **le formulaire** (форма, бланк)\n"
            "- **le dossier** (досье, папка документов)"
        )
        result = markdown_to_telegram_html(text)
        assert "<b>la préfecture</b>" in result
        assert "<b>le formulaire</b>" in result
        assert "(префектура)" in result
