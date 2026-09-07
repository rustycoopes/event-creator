# Slice 1 — Archive handling

> Part of the `whatsapp-archive-import` feature. PRD: [`../PRD.md`](../PRD.md) · Technical design:
> [`../TDD.md`](../TDD.md)

**Delivers:** A WhatsApp export that arrives without a `.zip` extension, as a `.gz`, or renamed to
anything at all is detected by its content, unpacked, and processed end to end — via both the
manual Upload page and the watch-folder import.

> Published as [`rustycoopes/event-creator#47`](https://github.com/rustycoopes/event-creator/issues/47).

## What to build

A new pure module `app/core/archive.py` (no class, no protocol — plain functions, matching the
`message_filter` / `date_parser` idiom):

- `sniff_archive(content) -> "zip" | "gzip" | None` — magic-byte check (`zipfile.is_zipfile`
  on a `BytesIO`; `\x1f\x8b` prefix for gzip).
- `decode_text(content) -> str` — BOM-aware: UTF-16 BOM → `utf-16`; else `utf-8-sig` with
  `errors="replace"`; strip a leading `﻿`.
- `extract_chat_text(content, *, kind, source_name) -> ChatExtraction` — returns the decoded chat
  text, the chosen member name, and a short reason string; raises `ArchiveError` when no readable
  chat text can be produced.
- `MAX_DECOMPRESSED_BYTES = 20 * 1024 * 1024` and bounded-read enforcement.

**Member selection** (ZIP): skip directory entries, `__MACOSX/` paths, and `._`-prefixed
basenames; then exact `_chat.txt` (case-insensitive basename) → largest `*.txt` by
`(-file_size, name)` → first entry whose bytes `decode_text` to NUL-free text → else
`ArchiveError("The archive doesn't contain a readable chat text file.")`. gzip: bounded-decompress
the single stream, member name = `source_name` minus a trailing `.gz` (or `<source_name>.txt`).

**Runner Extract step** (`app/services/pipeline/runner.py`): replace the
`run.filename.lower().endswith(".zip")` branch with `sniff_archive(content)`. `None` → `SKIPPED`,
`conversation = decode_text(content)`. Otherwise call `extract_chat_text`; `ArchiveError` → step
`FAILED`, run `FAILED` ("Could not extract a chat from the archive."), return; success → step
`SUCCESS` with the reason string, `conversation = result.text`. `run.filename` is used only for
log wording ("(no .zip extension)" when the suffix genuinely wasn't `.zip`) and gzip naming —
never the decision.

**Upload endpoint** (`app/api/v1/upload.py`): drop `ALLOWED_EXTENSIONS` / the suffix check. After
the existing size + empty checks: `sniff_archive(content) is not None` → accept; else
`decode_text(content)` and reject `400 unsupported_file_type` if it contains `\x00`. Decode the
whole buffer, not a sample. `.csv` still passes and still `SKIPPED`s at Extract.

`import_pending_files.py` and the storage layer are untouched.

## Design notes

- Module placement and the "runner authoritative / upload advisory" seam:
  [`../../../adr/whatsapp-archive-import-detection-and-module.md`](../../../adr/whatsapp-archive-import-detection-and-module.md).
- 20 MB cap, bounded `read(cap+1)` for both zip and gzip, never trusting `ZipInfo.file_size` /
  gzip ISIZE; streaming deferred with a `ponytail:` note at the read site:
  [`../../../adr/whatsapp-archive-import-decompression-cap.md`](../../../adr/whatsapp-archive-import-decompression-cap.md).
- `.docx` / `.xlsx` / `.jar` / `.apk` / `.odt` all pass `is_zipfile` — the member-selection
  fall-through to `ArchiveError` is what stops XML reaching Gemini (PRD story 5).
- `decode_text` is applied at **both** the archive path and the runner's former
  `content.decode("utf-8", errors="replace")` line — a UTF-16 export mangled in the runner is the
  same failure this feature exists to fix. TDD §4.
- Constants are module-level, not `Settings` — TDD §7.

## Blocked by

None — can start immediately.

## Acceptance criteria

- [ ] A ZIP whose filename has no `.zip` extension is detected, unpacked, and produces events;
      the Extract step is `SUCCESS` and its log notes the missing extension.
- [ ] A `.gz` single-file export (with and without the `.gz` extension) is detected, decompressed,
      and produces events.
- [ ] A WhatsApp export ZIP containing `_chat.txt` plus media files extracts `_chat.txt`, not a
      media file — regardless of member order in the archive.
- [ ] A ZIP with `WhatsApp Chat with X.txt` and no `_chat.txt` extracts that `.txt`.
- [ ] A media-only archive (no readable text member) and a `.docx`-shaped ZIP both fail the run
      with "The archive doesn't contain a readable chat text file." — no Gemini call.
- [ ] A corrupt/truncated ZIP and an encrypted ZIP both fail the run with an understandable
      message.
- [ ] An archive whose chosen member exceeds 20 MB decompressed fails the Extract step with the
      size message and the run is `FAILED` (metadata claiming a smaller size does not bypass it).
- [ ] A plain `.txt` chat export still processes unchanged; the Extract step is `SKIPPED`.
- [ ] A UTF-16 (BOM) `.txt` export processes without mojibake.
- [ ] Upload page: an extensionless valid-ZIP file and a plain-text file with a nonsense extension
      both return `202`; a PNG (or any file with a NUL byte) returns `400 unsupported_file_type`;
      empty → `400`, oversized → `413`.

## Testing

- **`tests/test_archive.py`** (new, focused, no DB): all member-selection permutations,
  `__MACOSX`/`._` filtering, encrypted/truncated zip, gzip ±`.gz`, `.docx`-shaped zip →
  `ArchiveError`, over-cap via `monkeypatch` of `MAX_DECOMPRESSED_BYTES` to ~1 KB (never allocate
  20 MB), `decode_text` on UTF-8 / UTF-8-BOM / UTF-16-BOM, `sniff_archive` magic bytes.
  Synthesize archives in-test with `zipfile.ZipFile(io.BytesIO(), "w")` / `gzip.compress`.
- **`tests/test_pipeline_runner.py`**: 3 wiring cases only — extensionless zip → events +
  `SUCCESS`; gzip → events; over-cap → run `FAILED` with the size message. Pattern already at
  `test_pipeline_runner.py:142`.
- **`tests/test_upload_api.py`**: the sniff cases from the acceptance criteria. The sniff runs
  before the storage dependency, so existing storage/scheduler overrides are irrelevant to it.
- `tests/test_dispatch.py` / `tests/test_internal_pipeline_api.py`: **no changes** — detection is
  below that layer.

<!-- /to-implementation appends a "## Delivered" section here once this slice ships. -->

## Delivered (2026-09-07, issue #47, branch `feature/whatsapp-archive-import-slice-1`)

**What shipped:** New pure module `app/core/archive.py` (`sniff_archive`, `decode_text`,
`extract_chat_text` → `ChatExtraction` / `ArchiveError`, `MAX_DECOMPRESSED_BYTES = 20 MB`). The
runner's Extract step now classifies by content (`sniff_archive`) rather than
`run.filename.endswith(".zip")`; `run.filename` is used only for log wording ("… (no .zip
extension)") and the gzip member name. The former `_extract_zip` helper and the
`content.decode("utf-8", errors="replace")` line are gone — both replaced by the archive module,
so UTF-16 / BOM exports decode correctly on every path. The Upload endpoint drops
`ALLOWED_EXTENSIONS` / the suffix check; after the size + empty checks it accepts anything that
sniffs as an archive or `decode_text`s to NUL-free text, and returns `400 unsupported_file_type`
otherwise (whole buffer decoded, not a sample).

**Member selection (ZIP):** regular files only (skip dir entries, `__MACOSX/`, `._`-prefixed
basenames via `PurePosixPath`); exact `_chat.txt` (case-insensitive) → largest `*.txt` by
`(-file_size, name)` → a **dot-free** basename that decodes NUL-free → else
`ArchiveError("The archive doesn't contain a readable chat text file.")`.

**Divergences from the plan:**

- Step 3 of ZIP member selection was tightened from the spec's literal "first entry whose bytes
  `decode_text` to NUL-free text" to "first entry whose basename contains **no dot**". A
  `.docx`-shaped ZIP's members (`[Content_Types].xml`, `_rels/.rels`, …) are all NUL-free text, so
  the literal rule would have fed Office XML to Gemini and never reached the `ArchiveError` that
  AC #5 requires. Dot-free-basename still allows a genuinely extensionless chat file while
  excluding Office parts, media and dotfiles.
- The over-cap ("too large") message lands in the Extract step log; the run-level failure message
  stays the generic "Could not extract a chat from the archive." per the TDD (agreed with the
  user at `/to-implementation`).
- gzip decompression also rejects NUL-containing output as `ArchiveError` (a gzipped photo fails
  cleanly instead of sending replacement-char garbage downstream) — a small addition consistent
  with the module's "readable chat text or raise" contract.
- Upload page (`upload.html`): the `accept=".txt,.zip,.csv"` filter was removed (a renamed export
  is invisible to a filtered picker) and the copy / `unsupported_file_type` message reworded.
  `test_upload_page.py` updated to match. Not called out in the issue but required for "detected …
  via the manual Upload page".
- Encrypted-ZIP test fixture: Python's `zipfile` cannot *write* encrypted archives, so a small
  traditional-PKWARE-encrypted ZIP is baked as a base64 constant in `tests/test_archive.py`.

**Code review** (`/code-review`, high effort) flagged three items; two fixed in this slice:

- *Extract step caught only `ArchiveError`.* A renamed ZIP using a compression method `zipfile`
  can't inflate (deflate64, imploded) raises `NotImplementedError` from `ZipFile.open`, which
  would have escaped and left the run non-terminal (Cloud Tasks retry loop, no failure email).
  Fixed: `archive._extract_zip` now also converts `NotImplementedError` / `OSError` to
  `ArchiveError`, and the runner's Extract step has an `except Exception` backstop that fails the
  run terminally.
- *Non-archive path had no binary guard.* A `.zip`-named corrupt file (fails `is_zipfile`) or a
  photo reaching the runner via the watch-folder import (which has no upload-endpoint gate) was
  decoded to replacement-char garbage and sent to Gemini. Fixed: the `kind is None` branch now
  fails the run when the decoded text contains a NUL byte, matching the gzip path and the ADR's
  "the runner must be able to reject junk on its own regardless."
- *20 MB cap can reject a >20 MB text-only export arriving via the watch-folder path.* Working as
  designed — this is the explicit trade in the decompression-cap ADR and TDD Open Question #2
  (streaming `message_filter` is the deferred upgrade path, `ponytail:` note left in code). No
  issue filed, per TDD Open Question #3.

**Testing:** `tests/test_archive.py` (new, 27 cases, no DB), 4 wiring cases in
`tests/test_pipeline_runner.py`, sniff cases in `tests/test_upload_api.py`
(`test_upload_rejects_unsupported_extension` → `test_upload_rejects_binary_file`, now a real PNG).
Full `mypy app tests` clean; affected suites pass locally against QA Supabase. `test_dispatch.py`
/ `test_internal_pipeline_api.py` unchanged as planned. Slice 2 (`message_filter` locale dates)
untouched.
