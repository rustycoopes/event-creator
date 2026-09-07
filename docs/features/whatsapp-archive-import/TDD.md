# WhatsApp Archive Import — Technical Design

**Feature:** [`PRD.md`](PRD.md)
**Date:** 2026-09-07
**Status:** Draft

## Architecture at a Glance

- A new pure module `app/core/archive.py` owns everything about "is this an archive, and get the
  chat text out of it" — magic-byte sniffing, member selection, bounded decompression,
  BOM-aware decoding. No I/O, no DB, plain exceptions. ([ADR: detection-and-module](../../adr/whatsapp-archive-import-detection-and-module.md))
- The pipeline runner's **Extract step** is the single authoritative classifier: it sniffs the
  downloaded bytes by content (not `run.filename`), extracts if it's a ZIP/gzip, and writes the
  step log from a reason string the helper returns.
- The **Upload endpoint** keeps a synchronous fast-fail that calls the *same* `sniff_archive`
  helper, so a dragged-in photo still gets an immediate `400` instead of a `failed` run — but it
  is explicitly advisory; the watch-folder import path has no such gate and relies entirely on
  the runner.
- Decompression is bounded by an **enforced read** capped at 20 MB, sized for a 512 MiB Cloud Run
  instance — never by trusting `ZipInfo.file_size` or the gzip ISIZE trailer.
  ([ADR: decompression-cap](../../adr/whatsapp-archive-import-decompression-cap.md))
- `app/core/message_filter.py` gains locale-aware WhatsApp line-date parsing driven by an ordered
  format table, with day/month order **inferred once per file** (MDY on a tie).
  ([ADR: date-order-inference](../../adr/whatsapp-archive-import-date-order-inference.md))

## Design Decisions

### 1. Archive handling module & classification seam

See [ADR: detection-and-module](../../adr/whatsapp-archive-import-detection-and-module.md).

`app/core/archive.py` public surface:

```python
class ArchiveError(Exception): ...

@dataclass(frozen=True)
class ChatExtraction:
    text: str          # decoded chat text, ready for the date filter
    member_name: str    # for the step log
    reason: str         # e.g. "extracted _chat.txt", "decompressed gzip", "used largest .txt"

MAX_DECOMPRESSED_BYTES = 20 * 1024 * 1024

def sniff_archive(content: bytes) -> Literal["zip", "gzip"] | None: ...
def decode_text(content: bytes) -> str: ...
def extract_chat_text(content: bytes, *, kind: str, source_name: str) -> ChatExtraction: ...
```

Runner Extract step becomes:

```
kind = sniff_archive(content)
if kind is None:
    step -> SKIPPED ("not an archive; extraction skipped")
    conversation = decode_text(content)
else:
    try:
        result = extract_chat_text(content, kind=kind, source_name=run.filename)
    except ArchiveError as exc:
        step -> FAILED ([str(exc)]); run -> FAILED ("Could not extract a chat from the archive."); return
    step -> SUCCESS ([result.reason])
    conversation = result.text
```

`run.filename` is used **only** for the log wording (append "(no .zip extension)" when the suffix
genuinely wasn't `.zip`) and to derive the gzip member name — never for the extract/skip
decision.

### 2. Member selection (`extract_chat_text`, ZIP branch)

Order, all on the archive's regular-file entries (skip directory entries, and any entry whose
path contains `__MACOSX/` or whose basename — via `PurePosixPath(name).name`, never `os.path` —
starts `._`):

1. basename equals `_chat.txt` case-insensitively → use it.
2. else the entry whose basename ends `.txt`, chosen by `(-file_size, name)` so ties are
   deterministic → use it.
3. else the first entry whose bytes `decode_text` to text containing no NUL → use it.
4. else raise `ArchiveError("The archive doesn't contain a readable chat text file.")`.

Step 4 is what handles the `is_zipfile`-true-but-not-really cases the FastAPI review flagged —
`.docx`, `.xlsx`, `.jar`, `.apk`, `.odt` are all valid ZIPs; they contain no readable chat text,
so they fail cleanly with that message (PRD user story 5) rather than feeding XML to Gemini.

Encrypted/corrupt archives: `zipfile` raises inside `extract_chat_text`; it is caught and
re-raised as `ArchiveError` with a user-facing message (PRD story 7).

gzip branch: decompress with the bounded read, `decode_text` the result, member name is
`source_name` minus a trailing `.gz` (or `<source_name>.txt` if there was none). Reason
`"decompressed gzip"`.

### 3. Bounded decompression

See [ADR: decompression-cap](../../adr/whatsapp-archive-import-decompression-cap.md). ZIP:
`archive.open(name).read(MAX_DECOMPRESSED_BYTES + 1)`, reject `len > cap`. gzip:
`GzipFile(...).read(MAX_DECOMPRESSED_BYTES + 1)`, same. A `ponytail:` comment at the ZIP read
site names the streaming upgrade path (feed lines straight into `message_filter`) with the ADR as
reference.

### 4. Encoding (`decode_text`)

BOM-aware, applied at **both** the archive-extraction path and the runner's former
`content.decode("utf-8", errors="replace")` line (they must agree — a UTF-16 export mangled in
the runner is the same "garbage to the LLM" failure this feature kills):

- UTF-16 BOM (`\xff\xfe` / `\xfe\xff`) → `decode("utf-16")`.
- else `decode("utf-8-sig")` (strips a UTF-8 BOM) with `errors="replace"`.
- Strip a leading `﻿` if one survives, so `message_filter`'s `^`-anchored patterns match
  line 1.

This is a deliberate behaviour change for BOM/UTF-16 exports (previously silently corrupted).
Minor scope addition beyond the PRD's explicit list, justified by the feature's stated goal.

### 5. Upload endpoint fast-fail (`app/api/v1/upload.py`)

Replace the `ALLOWED_EXTENSIONS` / suffix check with, after the existing size + empty checks:

```
if sniff_archive(content) is not None:
    pass  # let the pipeline extract it
else:
    text = archive.decode_text(content)          # whole buffer, not a sample
    if "\x00" in text:
        raise HTTPException(400, "unsupported_file_type")
```

Decode the **whole** `content` (already fully in memory under the 10 MB cap; sub-ms) — no 8 KB
sample; a sample can bisect a multibyte sequence and false-reject valid text. `\x00`-in-decoded
is the binary signal (JPEG/PNG/HEIC/PDF all carry NUL early). `400` detail string unchanged so
existing client handling is untouched. `.csv` still passes (text, no NUL) and the Extract step
still `SKIPPED`s it.

`import_pending_files.py` and the storage layer are unchanged.

### 6. Locale-aware line-date parsing (`app/core/message_filter.py`)

See [ADR: date-order-inference](../../adr/whatsapp-archive-import-date-order-inference.md).

- `_FORMATS`: ordered list of `(compiled_regex, builder)` tuples. Each regex is `^`-anchored and
  matches the **entire** timestamp through its trailing separator (` - ` for Android, `] ` for
  iOS) with a sender-ish lookahead, so a message body containing a date is never mistaken for a
  new line. Covers: `D/M/YY[YY], HH:MM[ ]- `, `D/M/YY[YY], h:MM[ ]AM/PM - `,
  `[D/M/YY[YY], H:MM(:SS)?] `, `[YYYY-MM-DD, HH:MM:SS] `, with `.` / `-` / `/` date separators.
  The AM/PM separator character class is `[   ]` (iOS uses U+202F). Years accepted as 2
  (`%y`, pivot 2069) or 4 digits.
- `_infer_date_order(lines) -> "MDY" | "DMY"` — one pre-scan, rules per the ADR.
- `_parse_line_date(line, order)` — pure, takes the resolved order.
- `filter_messages_within_window(...)` computes the order once, then runs the existing keep/drop
  walk. Return type changes to a small dataclass `FilterResult(text: str, format_recognised: bool)`
  (or a `(str, bool)` tuple — implementer's call). The runner logs
  `"date format not recognised — kept full history"` when `format_recognised` is `False` *and*
  the text was returned unchanged.

### 7. Constants & config

Module-level, per the repo precedent (`MAX_UPLOAD_BYTES`, `DEFAULT_DATE_WINDOW_DAYS`). Nothing
goes into `app/core/config.Settings` — none of it is secret or environment-varying.
`MAX_DECOMPRESSED_BYTES` in `core/archive.py`; the sniff/decoding lives in `core/archive.py` too.

## Component / Data Flow

```
Upload page ──┐                                   ┌─ sniff_archive (advisory 400 on binary)
              ├─► POST /api/v1/upload ─────────────┤
              │                                    └─► storage.upload_file → ProcessingRun(PENDING)
Watch folder ─┴─► POST /api/v1/import-pending-files ──► ProcessingRun(PENDING) per file
                                                             │
                                        Cloud Tasks push ────�▼
                                   POST /internal/pipeline/run
                                   dispatch.run_pipeline_dispatch
                                        runner.run_pipeline
   ┌──────────────────────────────────────────────────────────────────────────┐
   │ Step 1 File Received: storage.download_file → content: bytes             │
   │ Step 2 Extract:                                                          │
   │    kind = archive.sniff_archive(content)                                 │
   │    kind is None → SKIPPED, conversation = archive.decode_text(content)   │
   │    else → archive.extract_chat_text(content, kind, run.filename)         │
   │           ArchiveError → step FAILED, run FAILED, return                 │
   │           ok → SUCCESS(reason), conversation = result.text              │
   │ Step 3 Filter by Date:                                                   │
   │    FilterResult = filter_messages_within_window(conversation, window)    │
   │    not recognised → log "kept full history"                             │
   │ Step 4 Gemini → Step 5 Parse → Step 6 Dedup & Save → notify             │
   └──────────────────────────────────────────────────────────────────────────┘
```

## Testing Approach

Assert on observable behaviour — the `processing_runs` / `processing_steps` rows a run produces,
events extracted, the HTTP status/detail the Upload endpoint returns, the value a pure function
returns. Never on regex internals.

| Seam | File (all existing except `test_archive.py`) | Cases |
|---|---|---|
| `core/archive.py` pure fns | **`tests/test_archive.py`** (new, focused, no DB) | exact `_chat.txt` vs largest `.txt` vs text-fallback vs media-only→`ArchiveError`; `__MACOSX/` + `._` filtering; encrypted zip; truncated zip; gzip with/without `.gz`; `.docx`-shaped zip → `ArchiveError`; over-cap (via `monkeypatch` of `MAX_DECOMPRESSED_BYTES` to ~1 KB, never allocate 20 MB); `decode_text` on UTF-8/UTF-8-BOM/UTF-16-BOM; `sniff_archive` magic bytes |
| `runner.run_pipeline` end-to-end | `tests/test_pipeline_runner.py` | keep 3 wiring cases only: extensionless zip → events land + Extract `SUCCESS`; gzip → events land; over-cap → run `FAILED` with the size message. Synthesize archives in-test with `zipfile.ZipFile(io.BytesIO(), "w")` / `gzip.compress` (pattern already at `test_pipeline_runner.py:142`) |
| `message_filter.filter_messages_within_window` | `tests/test_message_filter.py` | extend the input→expected table: iOS bracketed, iOS `AM/PM` + U+202F, Android 12h, `DD.MM.YYYY`, `>12` component locking an ambiguous order, unrecognised format → whole conversation + `format_recognised is False`; **plus a regression block re-asserting every current US-Android case** |
| `upload.upload_file` | `tests/test_upload_api.py` | extensionless valid-zip bytes → `202`; text with nonsense extension → `202`; PNG-header+NUL → `400 unsupported_file_type`; empty → `400` (regression); oversized → `413` (regression) |

Out of automated scope: real fixture archives on disk (all synthesized in-test); actual 20 MB
allocations; `test_dispatch.py` / `test_internal_pipeline_api.py` need **no** changes — detection
is below that layer, adding coverage there would be redundant.

## Open Questions

1. **`FilterResult` shape** — dataclass vs `(str, bool)` tuple. Implementer's call at
   `/to-implementation`; the WBS slice should just name that the signature changes.
2. **20 MB vs raising `--memory`** — the ADR sets 20 MB and leaves `--memory` as an unpulled
   knob. If QA testing shows real exports being rejected, the WBS "dates" slice is the wrong
   place to revisit — it'd be a follow-up infra change. Confirm 20 MB is acceptable before
   `/to-issues`.
3. **Streaming `message_filter`** — deferred (ADR). A `ponytail:` note is left in code; no issue
   is filed unless the user wants one tracked.
4. **Does `show_reviewed`-style "is the archive from WhatsApp at all" detection matter?** No —
   out of scope per PRD; the LLM handles non-WhatsApp chat text as free text and the date filter
   degrades to "keep everything".

## WBS shape (for `/to-wbs`)

Two slices, no shared code beyond `core/archive.py` being imported by both:

- **Slice 1 — archive handling.** `core/archive.py` (`sniff_archive`, `decode_text`,
  `extract_chat_text`, cap), runner Extract step rewrite, upload endpoint fast-fail,
  `tests/test_archive.py` + the 3 runner wiring cases + the upload sniff cases. Delivers:
  extensionless / gzip / renamed WhatsApp archives process end to end.
- **Slice 2 — locale dates.** `message_filter` `_FORMATS` table, `_infer_date_order`,
  `_parse_line_date(line, order)`, `FilterResult`, runner "kept full history" log line, the
  `test_message_filter` table extension + regression block. Delivers: iOS / DMY-locale exports
  get the recency window.

Slice 1 carries `decode_text` (both paths need it). Slices are independently shippable.
