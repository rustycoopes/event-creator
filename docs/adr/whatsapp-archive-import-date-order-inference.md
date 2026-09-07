# Day/month order is inferred per file, defaulting to MDY

**Status:** Proposed
**Date:** 2026-09-07
**Feature:** [`whatsapp-archive-import`](../features/whatsapp-archive-import/TDD.md)

## Context

`app/core/message_filter.filter_messages_within_window` keeps only the most recent `window_days`
of a conversation before the LLM sees it, anchored on the latest message timestamp. It recognises
exactly one WhatsApp line format today: US-Android `M/D/YY, HH:MM - Sender:`, parsed with
`strptime(..., "%m/%d/%y")`.

This feature widens it to iOS bracketed (`[YYYY-MM-DD, HH:MM:SS]`, `[D/M/YY, H:MM:SS]`), Android
12- and 24-hour, and dotted/dashed separators. Many of these carry an **ambiguous** numeric date:
`03/04/2024` is 4 March or 3 April depending on the exporting phone's locale, and WhatsApp does
not record which. ISO `YYYY-MM-DD` is unambiguous; a component `> 12` disambiguates a given line;
everything else needs a policy.

The current "no parseable dates → return the whole conversation unchanged" fallback is *safe* —
it over-includes. A wrong-but-parseable day/month order is **not** safe in the same way: every
line parses, just transposed, so the fallback never fires and the window silently keeps the wrong
slice (off by up to months).

## Decision

**Two passes, order resolved once per file:**

1. `_infer_date_order(lines) -> Literal["MDY", "DMY"]` scans every line with the format regexes
   and collects the `(first, second)` integer pair from each ambiguous slash/dot date:
   - any line with `first > 12` → `DMY`;
   - else any line with `second > 12` → `MDY`;
   - else (no decisive line, or a genuine conflict where both appear) → **`MDY`**, matching the
     current code's `%m/%d/%y` behaviour.
   ISO `YYYY-MM-DD` lines never participate in inference.

2. The resolved order is threaded as a parameter into `_parse_line_date(line, order)` — a
   function argument, **not** module-level state; `_parse_line_date` stays pure.
   `filter_messages_within_window` computes the order at the top and passes it down alongside the
   existing `window_days` / `anchor`. The continuation-line keep/drop walk is unchanged.

`filter_messages_within_window` returns a small result carrying whether any known format was
recognised, so the runner can log "kept the full history — date format not recognised" (user
stories 13/14). This is a signature change with one production caller.

The ambiguous-default risk is documented in the function docstring and the TDD as a known
"silent wrong window" limitation, acceptable for v1 because the window is a token-cost
optimisation, not a correctness mechanism — a wrong window costs extra Gemini tokens or a few
stale events, never data loss.

## Alternatives considered

- **`dateutil` with `dayfirst` inferred.** Rejected: too permissive — it parses date-like
  fragments out of message *bodies*, and a regex is still needed to find where the timestamp ends
  and the sender begins, at which point manual integer parsing is simpler and far faster over
  100k-line files.
- **User-configurable date format (Settings > Preferences).** Rejected for v1: a settings field +
  migration for a 7-day cut where precision barely matters. Not foreclosed — the same Settings
  page is already slated to make `window_days` configurable and could carry this too.
- **Always MDY (no inference).** Rejected: leaves every rest-of-world export (the majority of
  WhatsApp users) with a wrong window whenever no line happens to have a day > 12.
- **Always DMY when ambiguous** (rest-of-world majority). Rejected: silently changes behaviour
  for the existing US-Android corpus and its regression tests; `> 12` inference already catches
  most real DMY files, and MDY-on-tie preserves today's behaviour for the ambiguous remainder.

## Consequences

- Real-world iOS and DMY-locale exports get the recency window applied instead of dumping full
  history to Gemini every import.
- A file that is genuinely ambiguous *and* MDY-vs-DMY matters for the cutoff gets a possibly-wrong
  window — bounded impact, documented, no data loss.
- `_parse_line_date` gains a required `order` argument; `filter_messages_within_window`'s return
  type changes; both blast radii are one caller plus `tests/test_message_filter.py`.
- The format list is a data-driven `_FORMATS` table (ordered `(compiled_regex, builder)` tuples,
  first match wins) — adding a format later is a table row, and every pattern stays `^`-anchored
  and requires the full stamp through its trailing separator so a message body like
  `12/25/26 is Christmas` can never be misread as a new dated line.
