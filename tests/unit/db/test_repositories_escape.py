from adaptive_lang_study_bot.db.repositories import _escape_like


class TestEscapeLike:

    def test_no_special_chars(self):
        assert _escape_like("hello") == "hello"

    def test_percent_escaped(self):
        assert _escape_like("100%") == r"100\%"

    def test_underscore_escaped(self):
        assert _escape_like("a_b") == r"a\_b"

    def test_both_escaped(self):
        assert _escape_like("%_test_%") == r"\%\_test\_\%"

    def test_empty_string(self):
        assert _escape_like("") == ""

    def test_no_mutation_of_normal_chars(self):
        assert _escape_like("café résumé") == "café résumé"

    def test_consecutive_wildcards(self):
        assert _escape_like("%%__") == r"\%\%\_\_"
