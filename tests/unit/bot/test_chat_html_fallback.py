"""Verify HTML fallback only catches TelegramBadRequest, not all exceptions."""

import inspect

from adaptive_lang_study_bot.bot.helpers import send_html_safe


class TestChatHtmlFallback:

    def test_html_fallback_catches_specific_exception(self):
        """The HTML→plaintext fallback must catch TelegramBadRequest, not bare Exception."""
        source = inspect.getsource(send_html_safe)
        # The fallback block should reference TelegramBadRequest
        assert "TelegramBadRequest" in source, (
            "send_html_safe should catch TelegramBadRequest specifically, not bare Exception"
        )

    def test_no_bare_except_for_send_fallback(self):
        """The plaintext fallback should be guarded by TelegramBadRequest, not Exception."""
        source = inspect.getsource(send_html_safe)
        lines = source.split("\n")
        for i, line in enumerate(lines):
            if "parse_mode=None" in line:
                # Look backwards for the except clause
                for j in range(i - 1, max(i - 5, 0), -1):
                    if "except " in lines[j]:
                        assert "TelegramBadRequest" in lines[j], (
                            f"Fallback parse_mode=None is guarded by '{lines[j].strip()}' "
                            "instead of 'except TelegramBadRequest'"
                        )
                        break
                break
