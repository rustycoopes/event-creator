"""Filter a WhatsApp export down to a recent message window (pipeline step 3, #52).

A real WhatsApp export is the *entire* chat history, which can span months or years. Re-extracting
the whole thing on every upload would repeatedly surface long-past agreements, so the pipeline
keeps only the most recent ``window_days`` of messages before handing the text to the LLM.

The window is measured relative to the **latest message in the conversation** (its own most recent
timestamp), not wall-clock "now": an export is uploaded some time after the conversation happened,
and anchoring on the file's own last message keeps the behaviour deterministic and testable
regardless of when the upload lands. The window length is a parameter (default 7 days); Slice 5's
Settings > Preferences will make it user-configurable, and the real-Gemini end-to-end test passes a
wide window so it reproduces the full example output.

WhatsApp export lines look like ``M/D/YY, HH:MM - Sender: text``. Lines without a leading date are
continuations of the preceding message and inherit its keep/drop decision; any header lines before
the first dated line are kept.
"""

import re
from datetime import date, datetime, timedelta

# Leading "M/D/YY, HH:MM" stamp of a WhatsApp message line (day/month unpadded). Only the date is
# captured; the time isn't needed for a day-granularity window.
_LINE_DATE_RE = re.compile(r"^(\d{1,2}/\d{1,2}/\d{2}), \d{1,2}:\d{2}")


def _parse_line_date(line: str) -> date | None:
    match = _LINE_DATE_RE.match(line)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), "%m/%d/%y").date()
    except ValueError:
        return None


def filter_messages_within_window(
    conversation: str, window_days: int = 7, anchor: date | None = None
) -> str:
    """Return ``conversation`` with only messages within ``window_days`` of the latest message.

    ``anchor`` defaults to the most recent dated message in the text; a message is kept when its
    date falls in the window ``(anchor - window_days, anchor]`` — i.e. strictly newer than
    ``anchor - window_days`` and no later than the anchor (so ``window_days=7`` keeps the final 7
    days inclusive of the anchor day). With the default anchor nothing is newer than it, so the
    upper bound only matters when a caller passes an explicit earlier anchor. If the text has no
    parseable dates at all, it's returned unchanged — better to over-include than to silently drop
    an unrecognised format.
    """
    lines = conversation.splitlines()
    dated = [d for d in (_parse_line_date(line) for line in lines) if d is not None]
    if not dated:
        return conversation

    effective_anchor = anchor if anchor is not None else max(dated)
    cutoff = effective_anchor - timedelta(days=window_days)

    kept_lines: list[str] = []
    keep_current = True  # header lines before the first dated line are kept
    for line in lines:
        line_date = _parse_line_date(line)
        if line_date is not None:
            keep_current = cutoff < line_date <= effective_anchor
        if keep_current:
            kept_lines.append(line)
    return "\n".join(kept_lines)
