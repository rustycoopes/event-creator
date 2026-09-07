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
