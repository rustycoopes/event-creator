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

WhatsApp export lines look like ``M/D/YY, HH:MM - Sender: text`` (US Android), ``D/M/YYYY, HH:MM -
Sender:`` (rest-of-world Android), or ``[YYYY-MM-DD, HH:MM:SS] Sender:`` / ``[D/M/YY, H:MM:SS AM/PM]
Sender:`` (iOS). Lines without a leading date are continuations of the preceding message and inherit
its keep/drop decision; any header lines before the first dated line are kept.

Slash/dot dates are locale-ambiguous (``03/04/26`` is 4 March or 3 April). The day/month order is
inferred **once per file** and defaults to MDY on a tie — see
``docs/adr/whatsapp-archive-import-date-order-inference.md``. ``app/core/date_parser.py`` is a
different job (it parses Gemini's *output* ``resolved_date``) and is not touched here.
"""

import re
from dataclasses import dataclass
from datetime import date, timedelta

# AM/PM is separated from the time by an ordinary space, U+00A0 (no-break) or U+202F (narrow
# no-break, what iOS emits).
_APM = r"[   ]?(?:[AaPp][Mm])"

# Ordered list of (compiled regex, is_iso) — first match wins. Every pattern is ``^``-anchored and
# consumes the *entire* timestamp through its trailing separator (`` - `` Android, ``] `` iOS) plus a
# non-space lookahead for the sender, so a message body like ``12/25/26 is Christmas`` is never read
# as a new dated line. For the non-ISO patterns groups are (first, second, year); for the ISO
# pattern they are (year, month, day).
_FORMATS: list[tuple[re.Pattern[str], bool]] = [
    # iOS ISO bracketed: [2026-08-30, 14:12:00] Name:
    (re.compile(r"^\[(\d{4})-(\d{2})-(\d{2}), \d{1,2}:\d{2}:\d{2}\] (?=\S)"), True),
    # iOS ambiguous bracketed: [30/8/26, 2:12:00 PM] Name:  /  [8.30.2026, 14:12] Name:
    (
        re.compile(
            r"^\[(\d{1,2})[./-](\d{1,2})[./-](\d{2,4}), \d{1,2}:\d{2}(?::\d{2})?(?:" + _APM + r")?\] (?=\S)"
        ),
        False,
    ),
    # Android 12-hour: 8/30/26, 2:12 PM - Name:
    (re.compile(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{2,4}), \d{1,2}:\d{2}" + _APM + r" - (?=\S)"), False),
    # Android 24-hour: 8/30/26, 14:12 - Name:
    (re.compile(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{2,4}), \d{1,2}:\d{2} - (?=\S)"), False),
]


@dataclass(frozen=True)
class _LineDate:
    a: int  # first date component (month if MDY, day if DMY; already month for ISO)
    b: int  # second date component (day if MDY, month if DMY; already day for ISO)
    year: int  # 2- or 4-digit as written
    iso: bool  # True => a/b are month/day already, don't apply inferred order


@dataclass(frozen=True)
class FilterResult:
    text: str
    format_recognised: bool  # did any line match a known WhatsApp timestamp format


def _match_line(line: str) -> _LineDate | None:
    for pattern, is_iso in _FORMATS:
        m = pattern.match(line)
        if m is None:
            continue
        g1, g2, g3 = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if is_iso:  # groups are (year, month, day)
            return _LineDate(a=g2, b=g3, year=g1, iso=True)
        return _LineDate(a=g1, b=g2, year=g3, iso=False)  # groups are (first, second, year)
    return None


def _normalise_year(year: int) -> int:
    if year >= 100:
        return year
    return 2000 + year if year < 69 else 1900 + year  # %y pivot: 69-99 -> 19xx


def _infer_date_order(lines: list[str]) -> str:
    """Resolve day/month order for the whole file. Any ambiguous line with a first component > 12
    forces DMY; otherwise MDY (covers "second > 12 -> MDY", genuine conflict, and undecidable —
    all of which the ADR maps to MDY, matching the historical ``%m/%d/%y`` behaviour). ISO lines
    don't participate."""
    for line in lines:
        ld = _match_line(line)
        if ld is not None and not ld.iso and ld.a > 12:
            return "DMY"
    return "MDY"


def _parse_line_date(line: str, order: str) -> date | None:
    ld = _match_line(line)
    if ld is None:
        return None
    if ld.iso or order == "MDY":
        month, day = ld.a, ld.b
    else:
        day, month = ld.a, ld.b
    try:
        return date(_normalise_year(ld.year), month, day)
    except ValueError:
        return None


def filter_messages_within_window(
    conversation: str, window_days: int = 7, anchor: date | None = None
) -> FilterResult:
    """Return ``conversation`` with only messages within ``window_days`` of the latest message.

    ``anchor`` defaults to the most recent dated message in the text; a message is kept when its
    date falls in the window ``(anchor - window_days, anchor]`` — i.e. strictly newer than
    ``anchor - window_days`` and no later than the anchor (so ``window_days=7`` keeps the final 7
    days inclusive of the anchor day). With the default anchor nothing is newer than it, so the
    upper bound only matters when a caller passes an explicit earlier anchor.

    ``FilterResult.format_recognised`` is ``False`` when no line matched any known WhatsApp
    timestamp format — the text is returned unchanged (better to over-include than to silently
    drop an unrecognised format) and the caller should note that the full history was kept.
    """
    lines = conversation.splitlines()
    order = _infer_date_order(lines)
    parsed = [_parse_line_date(line, order) for line in lines]
    dated = [d for d in parsed if d is not None]
    if not dated:
        return FilterResult(conversation, format_recognised=False)

    effective_anchor = anchor if anchor is not None else max(dated)
    cutoff = effective_anchor - timedelta(days=window_days)

    kept_lines: list[str] = []
    keep_current = True  # header lines before the first dated line are kept
    for line, line_date in zip(lines, parsed):
        if line_date is not None:
            keep_current = cutoff < line_date <= effective_anchor
        if keep_current:
            kept_lines.append(line)
    return FilterResult("\n".join(kept_lines), format_recognised=True)
