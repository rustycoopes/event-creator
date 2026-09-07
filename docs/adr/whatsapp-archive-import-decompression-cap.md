# Decompression is bounded by an enforced read, capped at 20 MB

**Status:** Proposed
**Date:** 2026-09-07
**Feature:** [`whatsapp-archive-import`](../features/whatsapp-archive-import/TDD.md)

## Context

The pipeline now decompresses archive content inside the Cloud Tasks push handler
(`internal_pipeline` → `runner.run_pipeline`), which runs on a Cloud Run instance deployed with
**no `--memory` flag — the 512 MiB default** (`.github/workflows/ci.yml` deploy step). The
existing `MAX_UPLOAD_BYTES = 10 MB` limit is on the *compressed* upload only. A crafted ZIP or
gzip stream expands far beyond that; a genuine multi-year heavy-user chat export can be 20–40 MB
of text.

At peak the handler can hold, simultaneously: the compressed bytes (≤10 MB), the decompressed
member, its decoded `str` (up to ~2–4× the byte size for CJK/Arabic/Hebrew — the very locales
this feature's date work targets), the filtered `str`, and the Gemini request payload. A 50 MB
decompressed member (the PRD's starting figure) realistically implies 300–900 MB resident and
OOMs a 512 MiB instance on its own.

Separately: `ZipInfo.file_size` and gzip's ISIZE trailer are both attacker-controlled archive
metadata — a pre-check against them is not a real control.

## Decision

- **`MAX_DECOMPRESSED_BYTES = 20 * 1024 * 1024` (20 MB)**, a module constant in
  `app/core/archive.py`.
- **Enforcement is on the actual read, not metadata:**
  - ZIP: `archive.open(name).read(MAX_DECOMPRESSED_BYTES + 1)`; if `len > cap`, raise
    `ArchiveError`. `ZipInfo.file_size` may still be read for the *cheap pre-check* and for
    ranking `.txt` members by size, but is never the sole guard.
  - gzip: `gzip.GzipFile(fileobj=io.BytesIO(content)).read(MAX_DECOMPRESSED_BYTES + 1)`; same
    over-cap check. The ISIZE trailer is never read.
- Over-cap fails the Extract step with a clear message ("The archive's contents are too large to
  process.") and fails the run terminally (`ProcessingRun` → `FAILED`), so a Cloud Tasks retry
  hits the already-terminal guard rather than repeating the work.
- The decoded/filtered strings are still materialised in full (not streamed) — see Alternatives.

## Alternatives considered

- **50 MB cap (PRD starting figure).** Rejected: OOMs the 512 MiB instance under realistic
  non-Latin exports, and 50 MB of chat text is ~10M+ tokens — far past any Gemini context window,
  so such a run can never succeed anyway; it would just fail expensively at the Gemini step
  instead of cheaply at Extract.
- **Trust `ZipInfo.file_size` / gzip ISIZE and skip the bounded read.** Rejected: both are
  attacker-controlled; a lying-small value plus a decompression bomb is the classic bypass.
- **Stream the member line-by-line into `message_filter`** (which is already line-oriented),
  never materialising the whole member or the whole `str`. This is the better long-term design
  and removes the two largest memory rows. Deferred, not adopted: it requires changing
  `filter_messages_within_window`'s signature from `str` to an iterable of lines and re-working
  its anchor calculation (currently `max()` over a list built in one pass), which is scope beyond
  "detect and extract the archive". Recorded as a `ponytail:` note at the read site with this
  ADR as the upgrade path. The 20 MB cap keeps memory bounded in the meantime.
- **Raise `--memory` on the Cloud Run services.** An in-repo infra change, still cheaper than the
  streaming refactor, and compatible with the platform's request-billing rule. Left as a knob the
  TDD flags but does not pull — 20 MB fits real exports without it.

## Consequences

- A genuinely enormous legitimate export (>20 MB decompressed) is rejected with a clear message
  rather than silently OOM-killing the worker mid-run. This is a deliberate
  reject-the-power-user-vs-instance-safety trade; 20 MB covers essentially every real
  media-stripped WhatsApp chat text.
- Memory stays bounded regardless of what the archive metadata claims.
- The cap is one constant to raise if `--memory` is later increased or streaming lands.
- `message_filter` still receives a full `str`; the streaming upgrade path is left open and
  marked in code.
