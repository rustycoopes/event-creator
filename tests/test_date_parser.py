"""Unit tests for app.core.date_parser.parse_earliest_date (issue #51).

The LLM's resolved_date is free text: one date, a date with a time, or several comma-separated
dates. parse_earliest_date reduces it to the single earliest calendar date (for sorting and the
calendar link) or None when nothing parseable is present. Values here mirror the real formats in
examples/example.lmmoutput.txt.
"""

import json
from datetime import date
from pathlib import Path

from app.core.date_parser import parse_earliest_date

_EXAMPLE_EVENTS = json.loads(
    (Path(__file__).resolve().parents[1] / "examples" / "example.lmmoutput.txt").read_text(
        encoding="utf-8"
    )
)


def test_parses_a_single_full_date() -> None:
    assert parse_earliest_date("Saturday 6 June 2026") == date(2026, 6, 6)


def test_ignores_the_time_of_day() -> None:
    assert parse_earliest_date("Sunday 31 May 2026 at 3:45 PM") == date(2026, 5, 31)


def test_multi_date_returns_the_earliest() -> None:
    assert parse_earliest_date("Sunday 7 June 2026, Monday 8 June 2026") == date(2026, 6, 7)


def test_multi_date_returns_the_earliest_regardless_of_listed_order() -> None:
    assert parse_earliest_date("Monday 8 June 2026, Sunday 7 June 2026") == date(2026, 6, 7)


def test_tbc_returns_none() -> None:
    assert parse_earliest_date("TBC") is None


def test_empty_or_whitespace_returns_none() -> None:
    assert parse_earliest_date("") is None
    assert parse_earliest_date("   ") is None


def test_unparseable_text_returns_none() -> None:
    assert parse_earliest_date("sometime soon") is None


def test_a_bare_weekday_without_a_date_is_ignored() -> None:
    # "Sunday" alone must not resolve to some arbitrary current-week date - only fragments that
    # actually carry a day/month/year count.
    assert parse_earliest_date("Sunday") is None


def test_a_parseable_fragment_is_used_even_when_another_is_unparseable() -> None:
    assert parse_earliest_date("TBC, Wednesday 10 June 2026") == date(2026, 6, 10)


def test_every_resolved_date_in_the_example_output_parses_to_a_date() -> None:
    # The 22 real LLM values in examples/example.lmmoutput.txt are all concrete dates, so each
    # must resolve (guards against a regression that silently returns None for a real format).
    for event in _EXAMPLE_EVENTS:
        assert parse_earliest_date(event["resolved_date"]) is not None, event["resolved_date"]


def test_example_multi_date_values_resolve_to_their_earliest() -> None:
    assert parse_earliest_date("Sunday 7 June 2026, Monday 8 June 2026") == date(2026, 6, 7)
    assert parse_earliest_date(
        "Thursday 18 June 2026, Monday 22 June 2026, "
        "Tuesday 23 June 2026, Wednesday 24 June 2026"
    ) == date(2026, 6, 18)
    assert parse_earliest_date("Sunday 28 June 2026, Monday 29 June 2026") == date(2026, 6, 28)
