"""Unit tests for the date-window message filter (pipeline step 3, #52).

Slice 2 (whatsapp-archive-import #48) widened the recognised line formats beyond US-Android and
changed the return type to ``FilterResult(text, format_recognised)``. The regression block at the
bottom re-asserts the pre-Slice-2 US-Android behaviour unchanged.
"""

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


def _text(conversation: str, **kwargs: object) -> str:
    return filter_messages_within_window(conversation, **kwargs).text  # type: ignore[arg-type]


# --- new Slice 2 formats -------------------------------------------------------------------------


def test_ios_iso_bracketed_export_gets_the_window_applied() -> None:
    conversation = "\n".join(
        [
            "[2026-08-01, 09:00:00] Russ Cooper: old ios message",
            "[2026-08-30, 14:12:00] Christine Cooper: recent ios message",
        ]
    )
    result = filter_messages_within_window(conversation, window_days=7)

    assert result.format_recognised is True
    assert "old ios message" not in result.text
    assert "recent ios message" in result.text


def test_ios_ambiguous_bracketed_with_narrow_no_break_space_am_pm_parses() -> None:
    # U+202F between time and PM, day component 30 -> locks DMY.
    conversation = "\n".join(
        [
            "[1/8/26, 9:00:00 AM] Russ Cooper: old",
            "[30/8/26, 2:12:00 PM] Christine Cooper: recent",
        ]
    )
    result = filter_messages_within_window(conversation, window_days=7)

    assert result.format_recognised is True
    assert "old" not in result.text
    assert "recent" in result.text


def test_android_12_hour_export_parses() -> None:
    conversation = "\n".join(
        [
            "8/1/26, 9:00 AM - Russ Cooper: old",
            "8/30/26, 2:12 PM - Christine Cooper: recent",
        ]
    )
    result = filter_messages_within_window(conversation, window_days=7)

    assert result.format_recognised is True
    assert "old" not in result.text
    assert "recent" in result.text


def test_dotted_dmy_export_parses_with_inferred_order() -> None:
    # "30.08.2026" -> first component 30 > 12 -> whole file is DMY.
    conversation = "\n".join(
        [
            "01.08.2026, 09:00 - Russ Cooper: old",
            "30.08.2026, 14:12 - Christine Cooper: recent",
        ]
    )
    result = filter_messages_within_window(conversation, window_days=7)

    assert result.format_recognised is True
    assert "old" not in result.text
    assert "recent" in result.text


def test_one_day_component_over_12_locks_the_whole_file_to_dmy() -> None:
    # "13/06/26" only parses as DMY (day 13), and no line votes MDY, so the whole file is DMY:
    # "04/07/26" is then 4 July 2026, not 7 April. Anchor = 4 July, so a 7-day window (cutoff
    # 27 June) drops the 13 June line.
    conversation = "\n".join(
        [
            "13/06/26, 09:00 - Russ Cooper: thirteenth of june",
            "04/07/26, 09:00 - Christine Cooper: fourth of july",
        ]
    )
    result = filter_messages_within_window(conversation, window_days=7)

    assert result.format_recognised is True
    assert "thirteenth of june" not in result.text
    assert "fourth of july" in result.text


def test_one_rogue_dmy_looking_body_line_does_not_flip_an_mdy_file() -> None:
    # An otherwise US-Android (MDY) export with a single pasted snippet whose day > 12 at column 0.
    # Majority vote keeps the file MDY, so the anchor stays 6/28 and the window is applied normally.
    conversation = "\n".join(
        [
            "6/1/26, 09:00 - Russ Cooper: old message",
            "6/28/26, 09:00 - Christine Cooper: recent message",
            "28/6/26, 09:00 - Someone: forwarded snippet inside a message body",
            "6/28/26, 09:05 - Russ Cooper: another recent message",
        ]
    )
    result = filter_messages_within_window(conversation, window_days=7)

    assert result.format_recognised is True
    assert "old message" not in result.text
    assert "recent message" in result.text
    assert "another recent message" in result.text


def test_unrecognised_format_returns_whole_conversation_and_flags_it() -> None:
    conversation = "2026.08.01 09:00 Russ | old\n2026.08.30 14:12 Christine | recent\n"
    result = filter_messages_within_window(conversation, window_days=7)

    assert result.format_recognised is False
    assert result.text == conversation


def test_message_body_containing_a_date_is_not_treated_as_a_new_dated_line() -> None:
    conversation = "\n".join(
        [
            "6/28/26, 09:00 - Russ Cooper: recent message",
            "12/25/26 is Christmas",
            "6/20/26, 09:00 - Christine Cooper: old message",
        ]
    )
    result = filter_messages_within_window(conversation, window_days=3)

    # The body line inherits the kept "recent message" decision instead of resetting keep/drop.
    assert "12/25/26 is Christmas" in result.text
    assert "old message" not in result.text


# --- regression: pre-Slice-2 US-Android behaviour, unchanged -----------------------------------


def test_keeps_only_messages_within_the_window() -> None:
    result = _text(_CONVERSATION, window_days=7)

    assert "old message" not in result
    assert "still old" not in result
    assert "recent message" in result
    assert "another recent message" in result


def test_continuation_lines_inherit_the_preceding_message_decision() -> None:
    result = _text(_CONVERSATION, window_days=7)

    assert "a continuation of the recent message" in result


def test_wider_window_keeps_everything() -> None:
    result = _text(_CONVERSATION, window_days=400)

    assert "old message" in result
    assert "another recent message" in result


def test_explicit_anchor_overrides_the_latest_message() -> None:
    result = _text(_CONVERSATION, window_days=7, anchor=date(2026, 6, 1))

    assert "old message" in result
    assert "still old" in result
    assert "recent message" not in result


def test_text_without_any_dates_is_returned_unchanged() -> None:
    text = "no timestamps here\njust some free text\n"
    result = filter_messages_within_window(text, window_days=7)

    assert result.text == text
    assert result.format_recognised is False
