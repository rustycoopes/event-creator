"""Reduce a human-readable ``resolved_date`` to its earliest calendar date (issue #51).

The Gemini extraction step returns ``resolved_date`` as free text that may name a single date
("Saturday 6 June 2026"), carry a time ("Sunday 31 May 2026 at 3:45 PM"), or list several,
comma-separated ("Sunday 7 June 2026, Monday 8 June 2026"). For sorting and the calendar link
(Slice 4) we store the single earliest date alongside the raw text; ``None`` when nothing
parseable is present ("TBC"). This is the shared parser the deduplicate-and-save pipeline step
(#52) calls per extracted event.
"""

from datetime import date

from dateutil import parser as dateutil_parser


def parse_earliest_date(resolved_date: str) -> date | None:
    """Return the earliest date named in ``resolved_date``, or ``None`` if none can be parsed.

    Multiple dates are comma-separated; each fragment is parsed independently (extra words like
    a weekday name or "at 3:45 PM" are tolerated) and the earliest is returned. A fragment with
    no digit - a bare weekday, "TBC", relative phrasing - is skipped rather than fuzzily resolved
    to an arbitrary current-week date.
    """
    earliest: date | None = None
    for raw_fragment in resolved_date.split(","):
        fragment = raw_fragment.strip()
        # Require a digit so a bare weekday / "TBC" / "sometime soon" isn't fuzzily coerced into
        # today's date by dateutil.
        if not any(char.isdigit() for char in fragment):
            continue
        try:
            parsed = dateutil_parser.parse(fragment, fuzzy=True).date()
        except (ValueError, OverflowError):
            continue
        if earliest is None or parsed < earliest:
            earliest = parsed
    return earliest
