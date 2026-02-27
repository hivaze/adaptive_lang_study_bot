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
