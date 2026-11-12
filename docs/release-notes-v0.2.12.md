# Field Manual Viewer Portable v0.2.12

**Released:** 11 November 2026

This release makes the library recoverable, faster to reopen, and safer to update.

## Highlights

- **Automatic archive recovery.** If `data/` is missing or the catalog database cannot be
  opened, the viewer rebuilds the catalog and search index from the original PDF and EPUB files
  in `archive/`. It never re-downloads files that are already present. Recovered entries are
  labelled **Recovered catalog**.
- **Cached-first startup.** A normal launch shows the saved catalog immediately. A background
  archive check then updates it; the reader and search reject stale content until the check
  catches up.
- **Disconnected-archive handling.** If the archive drive is unavailable, the last known catalog
  stays visible with a warning. Reconnecting the drive preserves the records.
- **Faster indexing.** New and rebuilt full-text indexes maintain a rowid lookup instead of
  scanning the whole FTS table. On the full-size test library this changed indexing from roughly
  one publication every 2.5 minutes to about ten publications per minute.
- **EPUB hardening.** Rejects unsafe ZIP members, compression bombs, XML entity declarations and
  path traversal in the EPUB spine.
- **Versioned portable builds.** The ZIP extracts to `Field Manual Viewer Portable v0.2.12`;
  the viewer header and Library Setup title show the version.
- **Versioned release naming.** The primary asset is
  `Field-Manual-Viewer-Portable-v0.2.12-Windows-x64.zip`. A byte-identical
  `Field-Manual-Viewer-Windows-x64.zip` alias keeps the stable "latest" link working.

## Install or upgrade

1. Download the versioned ZIP.
2. Extract it. The top-level folder is `Field Manual Viewer Portable v0.2.12`.
3. For a new library, run `Set-Up-Library.bat` and choose **Download All** (or
   **Download Test Batch (20)**).
4. For an existing library, close the viewer and Library Setup, then move the existing `archive/`
   folder into the new versioned folder. Move `data/` only if you want to keep the existing
   catalog and indexes; the viewer can rebuild both from the archive. Do not use **Download All**
   to recover an existing archive.

**Downgrade warning:** Database compatibility between v0.2.12 and v0.2.11 has not been
established. Do not install v0.2.12 over an existing library and then run v0.2.11 against the
same `data/` folder. Keep v0.2.11 and its library in a separate folder if you need a fallback.

## Verification

- Automated suite: 91 tests, 90 passed, 1 optional captured-source audit skipped.
- The release ZIP was rebuilt from the tagged source and compared file-by-file with the worktree.
- The ZIP contains 954 entries and no PDFs, EPUBs, databases, archive folders, tests or build
  tools.
- Packaged smoke test: extracted to a path with spaces, launched through
  `Start-Field-Manual-Viewer.bat`, answered `/api/health` with version `0.2.12`, served the
  versioned viewer header, and stopped cleanly.
- SHA-256: `8272ae858315b1970bff25a25b16d68a7bb57402cd09b7060fe8f18a664d0b2e`
- Size: 31,582,598 bytes.

## Assets

- `Field-Manual-Viewer-Portable-v0.2.12-Windows-x64.zip`
- `Field-Manual-Viewer-Portable-v0.2.12-Windows-x64.zip.sha256`
- `Field-Manual-Viewer-Windows-x64.zip` (byte-identical compatibility alias)
- `Field-Manual-Viewer-Windows-x64.zip.sha256`
- `release-notes-v0.2.12.md`

v0.2.11 remains available as the original release, unchanged.
