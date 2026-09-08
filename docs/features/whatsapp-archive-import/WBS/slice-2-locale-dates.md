# Slice 2 — Locale-aware date window

> Part of the `whatsapp-archive-import` feature. PRD: [`../PRD.md`](../PRD.md) · Technical design:
> [`../TDD.md`](../TDD.md)

**Delivers:** The recent-message window applies to WhatsApp exports from an iPhone, from a
`DD/MM/YYYY` locale, or with a 12-hour clock — not just US-Android exports — so those imports stop
sending the entire chat history to the LLM on every run.

> Published as [`rustycoopes/event-creator#48`](https://github.com/rustycoopes/event-creator/issues/48).

## What to build

Extend `app/core/message_filter.py`:

- **`_FORMATS`** — an ordered list of `(compiled_regex, builder)` tuples, first match wins. Each
  regex is `^`-anchored and matches the whole timestamp through its trailing separator (` - ` for
  Android, `] ` for iOS) with a sender-ish lookahead, so a message body like
  `12/25/26 is Christmas` is never read as a new dated line. Covers:
  - Android: `D/M/YY[YY], HH:MM[ ]- ` and `D/M/YY[YY], h:MM[ ]AM/PM - `
  - iOS: `[D/M/YY[YY], H:MM(:SS)?] ` and `[YYYY-MM-DD, HH:MM:SS] `
  - `.` / `-` / `/` date separators; AM/PM separator char class includes U+202F and U+00A0;
    years 2 (`%y`, pivot 2069) or 4 digits.
- **`_infer_date_order(lines) -> "MDY" | "DMY"`** — one pre-scan: any line with first component
  `> 12` → DMY; else any with second `> 12` → MDY; else (undecidable or conflicting) → `MDY`.
  ISO lines don't participate.
- **`_parse_line_date(line, order)`** — pure, takes the resolved order.
- **`filter_messages_within_window(...)`** computes the order once at the top, then runs the
  existing keep/drop walk unchanged. Its return type changes to carry whether any known format
  was recognised — a small dataclass `FilterResult(text, format_recognised)` or a `(str, bool)`
  tuple (implementer's call).
- **Runner** (`app/services/pipeline/runner.py`, Filter-by-Date step): when
  `format_recognised` is `False` and the text came back unchanged, add a step log line
  ("date format not recognised — kept the full conversation history").

## Design notes

- Per-file order inference, the MDY tie-break, and the "silent wrong window is bounded — it's a
  token-cost optimisation, not a correctness mechanism" reasoning:
  [`../../../adr/whatsapp-archive-import-date-order-inference.md`](../../../adr/whatsapp-archive-import-date-order-inference.md).
- Patterns must stay strict — the biggest correctness risk is a message body being misread as a
  new timestamped line, which would wrongly reset the keep/drop state mid-message. TDD §6.
- `app/core/date_parser.py` is **not** touched — it parses Gemini's *output* `resolved_date` via
  `dateutil`, a different job, explicitly out of scope (PRD Out of Scope).
- Shares `app/core/archive.py` with Slice 1 only by import; the only file both slices edit is
  `runner.py` (Slice 1: Extract step; this slice: Filter step) — minor merge coordination, no
  hard dependency.

## Blocked by

None — independent of Slice 1
([`#47`](https://github.com/rustycoopes/event-creator/issues/47)). If both are in flight, whichever
`runner.py` change lands second rebases the other's step edit.

## Acceptance criteria

- [ ] An iOS bracketed export (`[2026-08-30, 14:12:00] Name: …`) gets the window applied.
- [ ] An iOS `[D/M/YY, H:MM:SS AM/PM]` export with the U+202F narrow no-break space parses.
- [ ] An Android 12-hour export (`M/D/YY, h:MM AM/PM - Name:`) parses.
- [ ] A `DD.MM.YYYY` export parses with the order inferred.
- [ ] A file where one line's day component is `> 12` locks the whole file to DMY.
- [ ] An export in a still-unrecognised format returns the whole conversation unchanged and the
      run's Filter step logs that the full history was kept.
- [ ] Every existing US-Android `test_message_filter` case still passes (regression block).
- [ ] A message body containing a date (`12/25/26 is Christmas`) is not treated as a new dated
      line.

## Testing

- **`tests/test_message_filter.py`**: extend the existing input→expected-kept table with the
  cases above, plus an explicit regression block re-asserting the current US-Android behaviour.
  No new module.
- **`tests/test_pipeline_runner.py`**: one case that an unrecognised-format conversation produces
  the "kept full history" step log line.

<!-- /to-implementation appends a "## Delivered" section here once this slice ships. -->

## Delivered (2026-09-07, issue #48, branch `feature/whatsapp-archive-import-locale-dates`)

Shipped as planned. `app/core/message_filter.py` now recognises iOS bracketed exports (ISO
`[YYYY-MM-DD, HH:MM:SS]` and ambiguous `[D/M/YY, H:MM:SS AM/PM]`), Android 12- and 24-hour lines,
and `.`/`-`/`/` date separators, via an ordered `_FORMATS` table of `(compiled_regex, is_iso)`
tuples (first match wins). Every pattern is `^`-anchored and consumes the whole timestamp through
its trailing separator (` - ` Android, `] ` iOS) plus a `(?=\S)` sender lookahead, so a message
body like `12/25/26 is Christmas` is never read as a new dated line. `_infer_date_order` resolves
the day/month order once for the whole file by majority vote over the ambiguous lines (DMY only
when strictly more lines can *only* be DMY than can *only* be MDY; otherwise MDY, matching the
historical `%m/%d/%y` tie-break); `_parse_line_date(line, order)` stays pure and takes the resolved
order. AM/PM separator char class includes U+202F and U+00A0; 2-digit years use the `%y` pivot
(69–99 → 19xx). The "silent wrong window" limitation for genuinely-ambiguous DMY exports is
documented in the `filter_messages_within_window` docstring per the ADR.

`filter_messages_within_window` now returns `FilterResult(text, format_recognised)` (dataclass, not
a tuple). The pipeline runner's Filter-by-Date step logs `"date format not recognised — kept the
full conversation history"` when `format_recognised` is `False`.

Divergences from the plan:

- `_infer_date_order` uses a **majority vote** (DMY only when strictly more lines vote DMY than
  MDY) rather than the ADR's literal "any line with first component > 12 → DMY". Code review
  flagged that the ADR's any-line rule lets one rogue `28/6/26, …`-shaped line pasted into a
  message body flip an entire US-Android export to DMY; the vote keeps a lone outlier from
  overriding the file. Same result as the ADR for every real single-locale export.
- `filter_messages_within_window` matches each line through the regex table **once** (into a
  `list[_LineDate | None]`) and derives both the order and the per-line dates from it, instead of
  scanning every line twice (once in `_infer_date_order`, once when parsing) — matters on the
  ~100k-line exports the module is built for.
- `format_recognised` means "a known timestamp format matched at least one line" (regex match),
  not "at least one date parsed" — so the runner's "date format not recognised" log can't fire
  for a file whose format *was* recognised but whose dates all failed to resolve.
- No changelog line: event-creator has no `docs/changelog.md` (unlike organize-me); the Delivered
  section is the delivery record here.

`app/core/date_parser.py` untouched (out of scope). Tests: `tests/test_message_filter.py` extended
with the new-format table + a regression block re-asserting the pre-Slice-2 US-Android behaviour
(all pass locally); `tests/test_pipeline_runner.py` gained one unrecognised-format case asserting
the "kept full history" step log (DB-backed — runs in CI against Supabase QA, not locally
reachable). `mypy app tests` clean.
