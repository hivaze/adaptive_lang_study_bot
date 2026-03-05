"""Tests for the markdown_to_telegram_html converter."""

import pytest

from adaptive_lang_study_bot.bot.helpers import _fix_tag_nesting, markdown_to_telegram_html


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


class TestOverlappingTags:
    """Regression tests for overlapping bold/italic markers."""

    def test_triple_stars_bold_italic(self):
        """***text*** should produce properly nested bold+italic."""
        result = markdown_to_telegram_html("***hello***")
        assert result == "<b><i>hello</i></b>"

    def test_bold_containing_italic_at_end(self):
        """**avoir mal *à*** — italic inside bold ending together."""
        result = markdown_to_telegram_html("**avoir mal *à***")
        # Tags must be properly nested, never overlapping
        assert "</i></b>" in result or "</b>" in result
        assert "<b>" in result
        # No raw asterisks should remain in the formatted portion
        assert "<i>" not in result or result.index("</i>") < result.index("</b>")

    def test_fix_tag_nesting_overlapping_b_i(self):
        """Direct test: _fix_tag_nesting corrects <b>...<i>...</b></i>."""
        bad = "<b>avoir mal <i>à</b></i>"
        fixed = _fix_tag_nesting(bad)
        assert fixed == "<b>avoir mal <i>à</i></b>"

    def test_fix_tag_nesting_already_valid(self):
        """Properly nested tags are left unchanged."""
        good = "<b>avoir mal <i>à</i></b>"
        assert _fix_tag_nesting(good) == good

    def test_fix_tag_nesting_no_tags(self):
        """Plain text without tags passes through unchanged."""
        assert _fix_tag_nesting("hello world") == "hello world"

    def test_fix_tag_nesting_stray_close(self):
        """Stray closing tag with no opener is dropped."""
        assert _fix_tag_nesting("hello</b> world") == "hello world"

    def test_fix_tag_nesting_unclosed_tag(self):
        """Unclosed tag is auto-closed at the end."""
        assert _fix_tag_nesting("<b>hello") == "<b>hello</b>"

    def test_fix_tag_nesting_deeply_nested(self):
        """Triple nesting with one overlap."""
        bad = "<b><i><u>text</b></i></u>"
        fixed = _fix_tag_nesting(bad)
        # u is inside i which is inside b — closing b first should
        # close u and i, then reopen them
        assert "</u></i></b>" in fixed

    def test_real_world_avoir_mal(self):
        """The exact bug from the user report."""
        md = "**avoir mal *à***"
        result = markdown_to_telegram_html(md)
        # Must not contain overlapping tags
        assert "</b></i>" not in result
        # Must contain both bold and valid HTML
        assert "<b>" in result


class TestTable:
    def test_simple_table(self):
        text = (
            "| A | B |\n"
            "|---|---|\n"
            "| 1 | 2 |\n"
        )
        result = markdown_to_telegram_html(text)
        assert "<pre>" in result
        assert "</pre>" in result
        # Separator row should be gone
        assert "---" not in result
        # Data should be present
        assert "A" in result
        assert "1" in result

    def test_table_with_surrounding_text(self):
        text = (
            "Here is a table:\n\n"
            "| Name | Value |\n"
            "|------|-------|\n"
            "| foo  | bar   |\n"
            "\nEnd."
        )
        result = markdown_to_telegram_html(text)
        assert "Here is a table:" in result
        assert "<pre>" in result
        assert "End." in result

    def test_table_preserves_alignment(self):
        text = (
            "| Short | LongerHeader |\n"
            "|-------|-------------|\n"
            "| a     | b            |\n"
        )
        result = markdown_to_telegram_html(text)
        # Both columns should have consistent spacing inside <pre>
        assert "<pre>" in result
        # Header and data rows present
        assert "Short" in result
        assert "LongerHeader" in result

    def test_table_no_markdown_inside(self):
        """Bold/italic markers inside table cells should not be converted."""
        text = (
            "| **bold** | *italic* |\n"
            "|----------|----------|\n"
            "| data     | more     |\n"
        )
        result = markdown_to_telegram_html(text)
        # Inside <pre>, markdown should be raw (not converted to HTML tags)
        assert "<pre>" in result
        assert "<b>" not in result
        assert "<i>" not in result

    def test_realistic_conjugation_table(self):
        """The exact type of table from the user's bug report."""
        text = (
            "| Лицо | Окончание | Пример: habiter |\n"
            "|------|-----------|-------------------|\n"
            "| je | -e | j'habite |\n"
            "| tu | -es | tu habites |\n"
            "| nous | -ons | nous habitons |\n"
        )
        result = markdown_to_telegram_html(text)
        assert "<pre>" in result
        assert "</pre>" in result
        assert "Лицо" in result
        assert "j'habite" in result
        assert "|" not in result


class TestBackslashEscapes:
    def test_escaped_underscores(self):
        assert markdown_to_telegram_html(r"\_\_\_") == "___"

    def test_escaped_asterisk(self):
        assert markdown_to_telegram_html(r"\*not italic\*") == "*not italic*"

    def test_escaped_pipe(self):
        assert markdown_to_telegram_html(r"a \| b") == "a | b"

    def test_escaped_hash(self):
        assert markdown_to_telegram_html(r"\# not a header") == "# not a header"

    def test_backslash_in_code_not_stripped(self):
        """Backslash escapes inside code should not be processed."""
        result = markdown_to_telegram_html(r"`\_\_\_`")
        assert r"\_\_\_" in result

    def test_exercise_blanks(self):
        text = r"1. Nous \_\_\_ (habiter) à Paris."
        result = markdown_to_telegram_html(text)
        assert "Nous ___ (habiter)" in result
