"""Unit tests for the Google Calendar / Google Tasks link builders (ported from organize-me #54)."""

from datetime import date
from urllib.parse import parse_qs, urlparse

from app.core.calendar_url import build_google_calendar_url, build_google_tasks_url


def test_calendar_url_includes_action_title_and_dates() -> None:
    url = build_google_calendar_url(
        title="Christine to pick up Lucy.",
        event_date=date(2026, 6, 7),
        raw_date_text="Sunday",
        agreed_by=["Russ Cooper", "Christine Cooper"],
    )

    assert url is not None
    assert url.startswith("https://calendar.google.com/calendar/render?")
    query = parse_qs(urlparse(url).query)
    assert query["action"][0] == "TEMPLATE"
    assert query["text"][0] == "Christine to pick up Lucy."
    # All-day event: end date is start+1 (Google Calendar's exclusive-end-date convention).
    assert query["dates"][0] == "20260607/20260608"


def test_calendar_url_details_include_raw_date_text_and_agreed_by() -> None:
    url = build_google_calendar_url(
        title="Dentist",
        event_date=date(2026, 6, 6),
        raw_date_text="Saturday",
        agreed_by=["Russ", "Christine"],
    )

    assert url is not None
    query = parse_qs(urlparse(url).query)
    assert query["details"][0] == "Saturday\nAgreed by: Russ, Christine"


def test_calendar_url_omits_agreed_by_line_when_empty() -> None:
    url = build_google_calendar_url(
        title="Dentist", event_date=date(2026, 6, 6), raw_date_text="Saturday", agreed_by=[]
    )

    assert url is not None
    query = parse_qs(urlparse(url).query)
    assert query["details"][0] == "Saturday"


def test_calendar_url_returns_none_when_date_is_unresolved() -> None:
    url = build_google_calendar_url(
        title="TBC event", event_date=None, raw_date_text="next week sometime", agreed_by=[]
    )

    assert url is None


def test_calendar_url_handles_month_end_rollover() -> None:
    # 30 June + 1 day must roll into July, not produce an invalid 20260631.
    url = build_google_calendar_url(
        title="Month end", event_date=date(2026, 6, 30), raw_date_text="Tuesday", agreed_by=[]
    )

    assert url is not None
    query = parse_qs(urlparse(url).query)
    assert query["dates"][0] == "20260630/20260701"


def test_tasks_url_includes_title_and_due_date() -> None:
    url = build_google_tasks_url(title="Dentist appointment", due_date=date(2026, 6, 6))

    assert url is not None
    assert url.startswith("https://tasks.google.com/tasks/frontend?")
    query = parse_qs(urlparse(url).query)
    assert query["title"][0] == "Dentist appointment"
    assert query["due"][0] == "2026-06-06"


def test_tasks_url_returns_none_when_due_date_is_unresolved() -> None:
    url = build_google_tasks_url(title="TBC event", due_date=None)

    assert url is None
