"""Verify HTML fallback only catches TelegramBadRequest, not all exceptions."""

import inspect

from adaptive_lang_study_bot.bot.routers.chat import handle_text


class TestChatHtmlFallback:

    def test_html_fallback_catches_specific_exception(self):
        """The HTML→plaintext fallback must catch TelegramBadRequest, not bare Exception."""
        source = inspect.getsource(handle_text)
        # The fallback block should reference TelegramBadRequest
        assert "TelegramBadRequest" in source, (
            "chat.handle_text should catch TelegramBadRequest specifically, not bare Exception"
        )

    def test_no_bare_except_for_send_fallback(self):
        """The outer send loop should not have 'except Exception' before the fallback."""
        source = inspect.getsource(handle_text)
        # Find the section that does message.answer with parse_mode=None
        # It should be guarded by TelegramBadRequest, not Exception
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
