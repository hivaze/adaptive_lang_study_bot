import uuid

from adaptive_lang_study_bot.bot.routers.settings import _ALLOWED_SETVAL_FIELDS
from adaptive_lang_study_bot.utils import get_language_name


class TestSettingsSecurity:

    def test_allowed_fields_are_safe(self):
        dangerous_fields = {"tier", "level", "is_active", "telegram_id",
                            "streak_days", "weak_areas", "strong_areas",
                            "recent_scores", "onboarding_completed",
                            "native_language", "target_language"}
        assert not _ALLOWED_SETVAL_FIELDS & dangerous_fields

    def test_allowed_fields_only_preferences(self):
        assert _ALLOWED_SETVAL_FIELDS == {"preferred_difficulty", "session_style"}

    def test_tier_not_settable(self):
        assert "tier" not in _ALLOWED_SETVAL_FIELDS

    def test_is_active_not_settable(self):
        assert "is_active" not in _ALLOWED_SETVAL_FIELDS


class TestScheduleCallbackDataLength:
    """Telegram limits callback_data to 64 bytes."""

    def test_pause_callback_within_limit(self):
        sid = str(uuid.uuid4())
        data = f"sched:pause:{sid}"
        assert len(data.encode()) <= 64

    def test_resume_callback_within_limit(self):
        sid = str(uuid.uuid4())
        data = f"sched:resume:{sid}"
        assert len(data.encode()) <= 64

    def test_delete_callback_within_limit(self):
        sid = str(uuid.uuid4())
        data = f"sched:del:{sid}"
        assert len(data.encode()) <= 64

    def test_confirm_delete_callback_within_limit(self):
        sid = str(uuid.uuid4())
        data = f"sched:cdel:{sid}"
        assert len(data.encode()) <= 64

    def test_quiet_hours_callback_within_limit(self):
        data = "setqh:2200-0700"
        assert len(data.encode()) <= 64

    def test_max_notif_callback_within_limit(self):
        data = "setmn:5"
        assert len(data.encode()) <= 64

    def test_cta_words_callback_within_limit(self):
        data = "cta:words"
        assert len(data.encode()) <= 64

    def test_cta_session_callback_within_limit(self):
        data = "cta:session"
        assert len(data.encode()) <= 64


class TestFsrsCallbackDataLength:
    """FSRS review callback_data must stay within 64 bytes."""

    def test_show_callback_within_limit(self):
        # Worst case: large vocab ID, max position/total
        data = "fsrs:show:999999999:20:20"
        assert len(data.encode()) <= 64

    def test_rate_callback_within_limit(self):
        data = "fsrs:rate:999999999:20:20:4"
        assert len(data.encode()) <= 64


class TestDebugLogCallbackDataLength:
    """Debug log toggle callback_data must stay within 64 bytes."""

    def test_debug_log_callback_within_limit(self):
        data = "set:debug_log"
        assert len(data.encode()) <= 64


class TestLanguageNames:

    def test_known_code(self):
        assert get_language_name("en") == "English"
        assert get_language_name("fr") == "French"

    def test_unknown_code_uppercased(self):
        assert get_language_name("xx") == "XX"

    def test_all_supported_codes_have_names(self):
        codes = ["en", "ru", "fr", "es", "it", "de", "pt"]
        for code in codes:
            name = get_language_name(code)
            assert name != code.upper(), f"Missing name for {code}"
