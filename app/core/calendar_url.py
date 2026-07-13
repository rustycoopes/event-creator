"""Google Calendar + Google Tasks link builders for the events dashboard (ported from
organize-me's Slice 5.1, #54, to Event Creator in Slice R9).

Both builders return a URL the browser opens in a new tab to pre-fill a "quick add" form on
Google's own site - Event Creator never talks to the Calendar/Tasks APIs directly, so no extra
OAuth scope or credential is needed for this feature.

Google Calendar's ``render?action=TEMPLATE`` URL is a long-standing, widely used convention for
pre-filling a new event (title/dates/details) via a plain link - it's what an "Add to Google
Calendar" button on almost any event site uses.

Google Tasks has **no equivalent officially documented URL scheme** - Google has never published a
way to pre-fill a new task via a link. ``build_google_tasks_url`` below is a best-effort
``title``/``due`` query-string convention; whether Google's frontend actually honours it needs
manual verification against a real account. Even if Google ignores the params, the link still
opens Google Tasks - it just won't be pre-filled.
"""

from datetime import date as date_
from urllib.parse import urlencode

GOOGLE_CALENDAR_RENDER_URL = "https://calendar.google.com/calendar/render"
GOOGLE_TASKS_URL = "https://tasks.google.com/tasks/frontend"

_DATE_FORMAT = "%Y%m%d"


def build_google_calendar_url(
    *, title: str, event_date: date_ | None, raw_date_text: str, agreed_by: list[str]
) -> str | None:
    """A link that opens Google Calendar with a new all-day event pre-filled.

    Returns ``None`` when ``event_date`` is unset - an event with no resolvable date has nothing to
    put in the Calendar template's ``dates`` field. The Calendar ``details`` field carries the raw,
    unparsed date text plus who agreed - context a single parsed date column loses.
    """
    if event_date is None:
        return None
    start = event_date.strftime(_DATE_FORMAT)
    # Google Calendar's all-day `dates` param takes an exclusive end date, so a single-day event is
    # `start/start+1` - the convention behind every "Add to Calendar" button.
    end = date_.fromordinal(event_date.toordinal() + 1).strftime(_DATE_FORMAT)
    details_lines = [raw_date_text]
    if agreed_by:
        details_lines.append(f"Agreed by: {', '.join(agreed_by)}")
    params = {
        "action": "TEMPLATE",
        "text": title,
        "dates": f"{start}/{end}",
        "details": "\n".join(details_lines),
    }
    return f"{GOOGLE_CALENDAR_RENDER_URL}?{urlencode(params)}"


def build_google_tasks_url(*, title: str, due_date: date_ | None) -> str | None:
    """A best-effort link toward Google Tasks with a title/due-date query string.

    Returns ``None`` when ``due_date`` is unset, matching the Calendar builder's behaviour for
    events with no resolvable date.
    """
    if due_date is None:
        return None
    params = {"title": title, "due": due_date.isoformat()}
    return f"{GOOGLE_TASKS_URL}?{urlencode(params)}"
