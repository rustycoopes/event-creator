## Problem Statement

Users export a WhatsApp conversation and drop the file into their watch folder (or upload it
directly) to have Event Creator extract events from it. WhatsApp exports are `.zip` archives, but
in practice the file often arrives **without a `.zip` extension** — renamed by the user, stripped
by a share sheet, or handed over as an extensionless blob. Some arrive as a single-file `.gz`.

Today the pipeline decides whether to unzip purely from the filename: `runner.py` only extracts
when `run.filename` ends in `.zip`. An extensionless archive falls straight through to the
"treat the bytes as UTF-8 text" path, so the LLM is handed raw ZIP binary and the run produces
nothing useful (or fails). The manual Upload page is stricter still — it rejects anything that
isn't `.txt`/`.zip`/`.csv` at the door, so a renamed archive never even starts a run.

Separately, even when an archive *is* recognised and opened, `_extract_zip` returns the
**first** file in the archive. A WhatsApp export contains the chat text (`_chat.txt` on iOS, or
`WhatsApp Chat with X.txt` on Android) alongside any shared media, so "first file" can be a JPEG.

And once the chat text is in hand, the date-window filter (`message_filter`) only recognises one
WhatsApp line format — US-style `M/D/YY, HH:MM - Sender:`. iOS exports (`[YYYY-MM-DD, HH:MM:SS]`
/ `[D/M/YY, H:MM:SS]`), 12-hour clocks, and `DD.MM.YYYY` locale formats aren't parsed, so the
7-day window silently doesn't apply and the entire chat history is sent to the LLM every time.

## Solution

Detect archives by **content, not filename**, at the point every import path already converges —
the pipeline runner. If the downloaded bytes are a ZIP (or gzip) archive, route them through the
extract step regardless of what the file is called. Pick the chat text file out of the archive
deliberately instead of taking whatever comes first. Widen the Upload page to accept any file and
decide what it is by sniffing its bytes, matching the watch-folder path's leniency. Cap the
decompressed size so a crafted archive can't exhaust the worker.

Extend the date-window filter to recognise the common WhatsApp export line formats (iOS bracketed,
Android dash-separated, dotted/dashed locale dates, 12- and 24-hour clocks) and to infer
day/month order per file, so the recency window actually applies to real-world exports.

## User Stories

1. As a user, I want to drop a WhatsApp export into my watch folder even if it has no `.zip`
   extension, so that Event Creator still recognises it as an archive and processes it.
2. As a user, I want to upload a WhatsApp export from the Upload page even if I renamed it or it
   lost its extension, so that I don't have to rename it back to `.zip` first.
3. As a user, I want a single-file `.gz` export handled the same way as a `.zip`, so that a
   gzip-compressed chat still works.
4. As a user, I want the pipeline to pull the actual chat text out of a WhatsApp archive (not a
   photo that happened to be first), so that the LLM sees the conversation.
5. As a user, I want an archive that contains only media and no chat text to fail with a clear
   message, so that I know why nothing was extracted.
6. As a user, I want a plain `.txt` chat export to keep working exactly as before, so that this
   change doesn't regress the common case.
7. As a user, I want a corrupt or password-protected archive to fail the run with an
   understandable message, so that I'm not left staring at an empty result.
8. As a user, I want an enormous or maliciously-crafted archive to be rejected rather than hang
   or crash processing, so that one bad file doesn't take out my other pending files.
9. As a user, I want to upload a file that isn't an archive or readable text (e.g. an image) and
   be told immediately that it's not a supported file type, so that I don't wait for an async
   failure.
10. As a user with an iPhone-exported chat, I want the recency window to apply to my export, so
    that Event Creator doesn't re-surface months-old agreements on every import.
11. As a user in a `DD/MM/YYYY` locale, I want my export's dates read in the right order, so that
    the recency window keeps the right messages.
12. As a user with a chat exported in a 12-hour-clock format, I want the dates still recognised,
    so that the window still works.
13. As a user, I want an export in a line format Event Creator still can't parse to fall back to
    processing the whole history (as it does today) rather than dropping everything, so that I
    never get an empty extraction from a format misdetection.
14. As a user, I want the processing run's step log to show what happened ("detected a ZIP
    archive with no extension", "extracted `_chat.txt`", "decompressed gzip"), so that I can see
    why a run behaved the way it did.
15. As a developer, I want archive detection to live in exactly one place that both the upload
    and watch-folder import paths already flow through, so that the two paths can't drift.
16. As a developer, I want the extract step to be exercised by crafted in-memory archive bytes
    at the existing pipeline-runner test seam, so that the detection and member-selection logic
    is covered without real fixtures on disk.
17. As a developer, I want the locale date-format work confined to the one pure function that
    already owns line-date parsing, so that the change is a self-contained, heavily unit-tested
    diff.

## Implementation Decisions

**Scope:** `event-creator` repo only. No `organize-me` / chrome / other-app changes.

**Archive detection — pipeline runner, one place (`app.services.pipeline.runner`):**
- The Extract step (currently gated on `run.filename.lower().endswith(".zip")`) instead inspects
  the downloaded bytes: ZIP is detected via `zipfile.is_zipfile(io.BytesIO(content))`; gzip via
  the `\x1f\x8b` magic prefix. Filename extension is no longer consulted for the decision (it may
  still inform log wording).
- This covers both entry paths — manual upload and watch-folder import — because both create a
  `processing_runs` row and run through `run_pipeline`; no detection logic is duplicated in the
  API layer.
- Non-archive content: the step is marked `SKIPPED` exactly as today, and the bytes are decoded
  as UTF-8 (`errors="replace"`) unchanged.
- Formats supported: ZIP and single-file gzip. Not tar, rar, or 7z — an archive of one of those
  types is treated as non-archive content and will fail downstream with the existing
  unparseable-text behaviour (acceptable; no known need).

**Archive member selection (`_extract_zip` and a gzip sibling):**
- ZIP: choose the member named exactly `_chat.txt` (case-insensitive, basename only) if present;
  else the largest entry whose name ends `.txt`; else the first regular (non-directory) entry.
  Return its bytes and name.
- An archive with no regular files, or an unreadable/encrypted archive, raises — the step fails
  and the run fails with "Could not extract the uploaded archive." as today.
- gzip: decompress the single stream; the extracted name is the original filename with a trailing
  `.gz` removed, or `<original-name>.txt` if there's no `.gz` suffix. Content is then treated as
  text like any `.txt`.

**Decompression size guard:**
- Before materialising the chosen member, check its declared uncompressed size
  (`ZipInfo.file_size`; for gzip, decompress with a bounded read). If it exceeds a fixed cap
  (50 MB — comfortably above any real chat text, well below what would pressure the worker), the
  Extract step fails with a clear message ("The archive's contents are too large to process.")
  and the run fails. The cap is a module-level constant.
- This is a trust boundary: the existing 10 MB limit is on the *compressed* upload only, and a
  ZIP bomb expands far beyond it.

**Upload page — sniff instead of allow-list (`app.api.v1.upload.upload_file`):**
- The `ALLOWED_EXTENSIONS` extension check is removed. After the size check, the bytes are
  sniffed:
  - ZIP or gzip magic → accepted (the pipeline will extract).
  - Otherwise, decode a leading sample (first ~8 KB) as UTF-8. Clean decode → accepted as a text
    export. `UnicodeDecodeError`, or a sample dominated by NUL/control bytes → `400
    unsupported_file_type` (same error code as today, so existing client handling is unchanged).
- Empty-file and size-limit behaviour (`400 empty_file`, `413 file_too_large`, 10 MB cap) are
  unchanged.
- The watch-folder import path (`import_pending_files` / `StorageProvider.list_new_files`) needs
  no change — it already lists and enqueues files regardless of extension.

**Locale-aware line-date parsing (`app.core.message_filter`):**
- `_parse_line_date` (and the `_LINE_DATE_RE` it uses) is extended to recognise:
  - Android dash-separated: `D/M/YY[YY], HH:MM[ ]- Sender:` and `D/M/YY[YY], h:MM[ ]AM/PM - `.
  - iOS bracketed: `[D/M/YY[YY], H:MM:SS] Sender:` and `[YYYY-MM-DD, HH:MM:SS] Sender:`, tolerating
    the narrow no-break space iOS places before `AM`/`PM`.
  - Dotted/dashed date separators (`DD.MM.YYYY`, `DD-MM-YYYY`) in the above shapes.
  - Only the calendar date is needed (day-granularity window); the time portion is matched but
    discarded.
- Day/month order is inferred **per file**: scan all matched dates; if any date has a component
  `> 12`, that fixes the order for the whole file. If still ambiguous, assume `MDY` (matches the
  current code's `%m/%d/%y` default — deterministic, no config, no new setting).
- ISO `YYYY-MM-DD` is unambiguous and always read as such.
- The continuation-line rule is unchanged: a line with no recognised leading date inherits the
  preceding message's keep/drop decision; header lines before the first dated line are kept.
- Fallback is unchanged: if **no** line dates parse under any recognised format, the whole
  conversation is returned as-is (over-include rather than silently drop). A single pipeline step
  log line notes when this happens.

**Step logging:** the Extract step's `log_lines` gain wording distinguishing "detected ZIP
archive (no `.zip` extension)", "decompressed gzip", and which member was chosen. The Filter step
notes when the date format wasn't recognised and the full history was kept.

## Testing Decisions

A good test here asserts on externally-observable behaviour: the `processing_runs` /
`processing_steps` rows a run produces, the events extracted, the HTTP status/detail the Upload
endpoint returns, and the string a pure function returns — never on private helper internals or
regex structure.

**Seam 1 — `tests/test_pipeline_runner.py` (pytest, existing):** the module docstring already
declares `run_pipeline` the intended seam for full pipeline behaviour, and this file already
drives it with a fake storage provider and canned Gemini responses. New cases feed crafted
in-memory bytes as the "downloaded file":
- A ZIP archive whose `run.filename` has no `.zip` extension is detected, extracted, and
  processed to events; the Extract step is `SUCCESS` with the detection noted.
- A ZIP containing `_chat.txt` plus a dummy binary "photo" entry (photo first in the archive)
  extracts the chat text, not the photo.
- A ZIP with a `WhatsApp Chat with X.txt` and no `_chat.txt` picks the `.txt` file.
- A ZIP containing only media (no `.txt`, no obvious chat file) — the first-file fallback path,
  and separately an archive with zero regular files fails the run cleanly.
- A gzip stream with no `.gz` extension is detected, decompressed, and processed.
- A plain `.txt` (no archive magic) still processes unchanged and the Extract step is `SKIPPED`.
- An archive whose declared uncompressed size exceeds the cap fails the Extract step with the
  size message; the run is `failed`.
- A corrupt/truncated ZIP fails the Extract step with the extraction-error message.

**Seam 2 — `tests/test_message_filter.py` (pytest, existing):** `filter_messages_within_window`
is a pure function; this file already tables input conversations against expected kept output.
New cases:
- iOS bracketed format (`[2026-08-30, 14:12:00] Name: ...`) — window applied correctly.
- iOS `[D/M/YY, H:MM:SS AM/PM]` with the narrow no-break space — parsed.
- Android 12-hour `M/D/YY, h:MM AM/PM - Name:` — parsed.
- `DD.MM.YYYY` locale format — parsed, order inferred.
- A file where a `>12` component fixes an otherwise-ambiguous `DD/MM` vs `MM/DD` order.
- An unrecognised format still returns the whole conversation unchanged.
- The existing US-Android cases continue to pass (regression guard).

**Seam 3 — `tests/test_upload_api.py` (pytest + httpx, existing):** already exercises the Upload
endpoint's gating with `E2E_TEST_MODE`-style fakes. New cases:
- An extensionless file whose bytes are a valid ZIP → `202`, a run is created.
- A `.txt`-less plain-text file with a nonsense extension → `202`.
- A small binary file (e.g. PNG header + NUL bytes) → `400 unsupported_file_type`.
- Empty file → `400 empty_file` (regression), oversized → `413` (regression).

## Out of Scope

- tar / rar / 7z archives, nested archives, and multi-member archives with more than one chat
  text file (the largest `.txt` wins; no merging).
- Resolving `resolved_date` free text emitted by Gemini — that goes through `dateutil` in
  `app.core.date_parser` and is unaffected by this feature.
- Making the recency-window length or the day/month-order assumption user-configurable (a
  Settings > Preferences item; the window length is already slated for that separately).
- Detecting the *source* of an archive (WhatsApp vs Telegram vs other) or format-specific
  message parsing beyond the date line — the LLM handles message content as free text.
- Any change to `StorageProvider` implementations or the watch-folder listing/move behaviour.
- Virus/malware scanning of uploaded archives.

## Further Notes

- The pipeline was built around WhatsApp exports from the start (`message_filter`'s own
  docstring), so this feature hardens an existing happy path rather than opening new ground.
- The 50 MB decompression cap and the 8 KB sniff sample size are starting values; `/to-design`
  should sanity-check them against the largest realistic chat export and the existing 10 MB
  compressed limit.
- `/to-design` should pin the exact set of regex alternates for the line formats (this PRD lists
  the shapes, not the patterns) and confirm whether the per-file day/month inference is computed
  once up front or lazily — an implementation-precise detail better decided with a design pass.
- WBS is likely two slices: (1) content-based detection + member selection + size guard + Upload
  sniff (the "archive" half), (2) locale-aware line-date parsing (the "dates" half). They share
  no code and can ship independently.
