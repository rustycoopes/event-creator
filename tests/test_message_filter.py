"""Unit tests for the date-window message filter (pipeline step 3, #52)."""

from datetime import date

from app.core.message_filter import filter_messages_within_window

_CONVERSATION = "\n".join(
    [
        "5/30/26, 10:00 - Russ Cooper: old message well outside the window",
        "6/1/26, 09:30 - Christine Cooper: still old",
        "6/28/26, 09:00 - Russ Cooper: recent message",
        "this line has no date - a continuation of the recent message",
        "6/28/26, 09:05 - Christine Cooper: another recent message",
    ]
)


def test_keeps_only_messages_within_the_window() -> None:
    result = filter_messages_within_window(_CONVERSATION, window_days=7)

    # Anchor is the latest message (6/28); a 7-day window drops 5/30 and 6/1.
    assert "old message" not in result
    assert "still old" not in result
    assert "recent message" in result
    assert "another recent message" in result


def test_continuation_lines_inherit_the_preceding_message_decision() -> None:
    result = filter_messages_within_window(_CONVERSATION, window_days=7)

    # The undated continuation line follows a kept (recent) message, so it's kept too.
    assert "a continuation of the recent message" in result


def test_wider_window_keeps_everything() -> None:
    result = filter_messages_within_window(_CONVERSATION, window_days=400)

    assert "old message" in result
    assert "another recent message" in result


def test_explicit_anchor_overrides_the_latest_message() -> None:
    # Anchor on 6/1 with a 7-day window keeps 5/30 and 6/1 but drops the 6/28 messages.
    result = filter_messages_within_window(_CONVERSATION, window_days=7, anchor=date(2026, 6, 1))

    assert "old message" in result
    assert "still old" in result
    assert "recent message" not in result


def test_text_without_any_dates_is_returned_unchanged() -> None:
    text = "no timestamps here\njust some free text\n"

    assert filter_messages_within_window(text, window_days=7) == text
