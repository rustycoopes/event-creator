# Archive handling lives in a pure `core` module, classified authoritatively in the pipeline runner

**Status:** Proposed
**Date:** 2026-09-07
**Feature:** [`whatsapp-archive-import`](../features/whatsapp-archive-import/TDD.md)

## Context

WhatsApp exports are ZIP archives that frequently arrive without a `.zip` extension (or as
single-file gzip). Today the pipeline's Extract step decides whether to unzip from
`run.filename.lower().endswith(".zip")`, and the Upload endpoint rejects anything outside
`{.txt,.zip,.csv}` at the door. Two independent code paths reason about "what kind of file is
this" from the filename, and they disagree — the exact drift this feature exists to eliminate.

Two questions must be answered:
1. **Where does the classification + extraction logic live** — inline in `runner.py`, in the
   existing module-private `_extract_zip` helper, or in a new module?
2. **Which layer is authoritative** — does the runner decide, does the upload endpoint decide, or
   both?

The repo has a recent ADR (`dashboard-bulk-actions-no-service-layer`) that leans hard against
ceremony. `app/core/` already holds pure, I/O-free domain logic (`message_filter`, `date_parser`,
`security`). `app/api/v1/import_pending_files.py` already reaches into `app/api/v1/upload.py`
privates (`_prompt_text_for`), which we do not want to repeat.

## Decision

**A new `app/core/archive.py` of plain functions** (no class, no protocol):
- `sniff_archive(content: bytes) -> Literal["zip", "gzip"] | None` — magic-byte check only.
- `extract_chat_text(content: bytes, *, kind: str, source_name: str) -> ChatExtraction` —
  returns the chosen member's decoded text, the member name, and a short reason string; raises
  `ArchiveError` (a plain exception) when no readable chat text can be produced.
- `decode_text(content: bytes) -> str` — BOM-aware decode (`utf-8-sig`, UTF-16 via BOM, else
  `utf-8` with `errors="replace"`), used by both `extract_chat_text` and the runner's
  non-archive path.
- `MAX_DECOMPRESSED_BYTES` module constant.

These operate on `bytes`, raise plain exceptions, do no logging and touch no DB / `ProcessingStep`
row. The runner passes bytes in and writes the `log_lines` itself from the returned reason string.

**The pipeline runner's Extract step is the sole authoritative classifier.** Every path to Gemini
(`upload.py` and `import_pending_files.py` both dispatch via Cloud Tasks to
`internal_pipeline` → `dispatch.run_pipeline_dispatch` → `runner.run_pipeline`) runs through it;
there is no bypass.

**The Upload endpoint keeps an advisory fast-fail** that calls the *same* `sniff_archive` helper:
archive magic → accept; otherwise `decode_text` + reject (`400 unsupported_file_type`) if the
result contains a NUL byte. This is a UX convenience (synchronous error instead of a `failed` run
in `/logs`), explicitly not a second classifier — the watch-folder import path has no equivalent
gate and never can, so the runner must be able to reject junk on its own regardless.

Filename is removed from the decision path entirely. It may still be read for cosmetic log
wording ("detected ZIP archive with no `.zip` extension") and to derive the gzip member name
(strip a trailing `.gz`).

## Alternatives considered

- **Inline in `runner.py` / expand `_extract_zip`.** Laziest diff. Rejected: the Extract step
  would own format detection + member ranking + a zip-bomb defense inline, pushing `runner.py`
  past ~470 lines, and the member-selection heuristic (the part most likely to be wrong) would be
  testable only through the full `run_pipeline` seam with crafted archives and a fake storage
  provider. A pure module matches the existing `core` idiom and is not the "service layer" the
  no-service-layer ADR argues against — it's a function module like `message_filter`.
- **Runner-only, no upload sniff (accept anything non-empty).** Makes the two paths perfectly
  symmetric. Rejected: a dragged-in photo would create a `failed` run row the user has to notice
  in `/logs` rather than getting an immediate "not a supported file" — a real regression in the
  common mistake case.
- **Full symmetric byte-sniff in both places** (each independently decides text vs archive vs
  reject). Rejected: reintroduces two classifiers that can drift — the original sin.
- **Detection at the storage/download boundary.** Rejected: would duplicate a sniff across five
  `StorageProvider` subclasses or add a base-class post-download hook every provider inherits.
  "What is this payload" is a pipeline-domain question, not a transport concern.

## Consequences

- One place to change the list of recognised formats; the upload path and the runner cannot
  disagree about what an archive is because they call the same function.
- The zip-bomb guard, member-ranking heuristic, and encoding handling get fast, DB-free unit
  tests in a new `tests/test_archive.py`.
- `runner.py`'s Extract step shrinks to: sniff → (extract | decode) → write step log.
- The `ChatExtraction` / `ArchiveError` shapes are a new internal contract to keep stable.
- `decode_text` changing the runner's existing `content.decode("utf-8", errors="replace")` line
  is a deliberate behaviour change (UTF-16 / BOM exports stop being mangled) — see the TDD's
  Encoding decision.
- If a second consumer ever needs archive handling, it imports `core/archive.py` — never a
  cross-layer private import.
