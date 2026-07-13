"""Unit tests for the Getting Started onboarding checklist view-model (ported from organize-me
#56)."""

from app.core.onboarding import build_onboarding_steps, onboarding_complete
from app.models.user_settings import UserSettings


def _settings(storage: bool, notifications: bool, first_upload: bool) -> UserSettings:
    return UserSettings(
        onboarding_storage_done=storage,
        onboarding_notifications_done=notifications,
        onboarding_first_upload_done=first_upload,
    )


def test_three_steps_in_documented_order() -> None:
    steps = build_onboarding_steps(_settings(False, False, False))

    assert [s.label for s in steps] == [
        "Connect Storage",
        "Set Notification Preferences",
        "Upload First File",
    ]


def test_each_step_links_to_its_page() -> None:
    steps = build_onboarding_steps(_settings(False, False, False))

    assert {s.label: s.url for s in steps} == {
        "Connect Storage": "/settings",
        "Set Notification Preferences": "/settings",
        "Upload First File": "/upload",
    }


def test_step_done_state_reflects_the_user_booleans() -> None:
    steps = build_onboarding_steps(_settings(True, False, True))

    done = {s.label: s.done for s in steps}
    assert done["Connect Storage"] is True
    assert done["Set Notification Preferences"] is False
    assert done["Upload First File"] is True


def test_not_complete_while_any_flag_is_false() -> None:
    assert onboarding_complete(_settings(True, True, False)) is False
    assert onboarding_complete(_settings(False, False, False)) is False


def test_complete_only_when_all_three_flags_are_true() -> None:
    assert onboarding_complete(_settings(True, True, True)) is True
