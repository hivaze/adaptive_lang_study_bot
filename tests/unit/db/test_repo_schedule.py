"""Verify ScheduleRepo.recalculate_triggers_for_user calls update_fields (not update)."""

import inspect

from adaptive_lang_study_bot.db.repositories import ScheduleRepo


class TestScheduleRepoApiSurface:

    def test_recalculate_uses_update_fields(self):
        """Source of recalculate_triggers_for_user must call update_fields, not update."""
        source = inspect.getsource(ScheduleRepo.recalculate_triggers_for_user)
        assert "ScheduleRepo.update_fields(" in source
        assert "ScheduleRepo.update(" not in source.replace("ScheduleRepo.update_fields(", "")

    def test_schedule_repo_has_no_bare_update(self):
        """ScheduleRepo should not have a method named 'update' (only update_fields)."""
        assert not hasattr(ScheduleRepo, "update") or ScheduleRepo.update is ScheduleRepo.update_fields
