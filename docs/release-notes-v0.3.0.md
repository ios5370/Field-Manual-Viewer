# Field Manual Viewer Portable v0.3.0

**Released:** 20 November 2026

v0.3.0 adds native Markdown reading/search and an importer for the official T.REX ARMS
Field Manual Vault release. The application ZIP still contains **no manuals**.

## Highlights

- **Markdown reader and search.** Official `.md` releases open in a safe reflowed reading
  view, participate in local full-text search, and sit alongside PDF and EPUB on each card.
- **Field Manual Vault import.** `Import-Field-Manual-Vault.bat` reads the release
  `index.csv`, opens the per-topic ZIPs, verifies every member against its published SHA-256,
  and creates a canonical `archive/<publication-id>/<publication-id>.<format>` library.
- **Resumable and non-destructive.** Existing files with the same checksum are skipped. The
  source vault is read-only. Re-running the importer continues where it stopped.
- **Dynamic add/remove.** Files copied into `archive/` are discovered and indexed in the
  background. Removing one format removes that button from the card without deleting the
  catalog record.
- **Current catalog scale.** The 2026-11 source has **6,472 publications** and roughly
  **110 GB** of PDF + EPUB + Markdown. The portable app itself remains small.
- **Offline-first behavior.** Saved catalogs, readers and search stay available without the
  source website. Importing is an explicit operation.

## Adding files later

Use the canonical layout:

```text
archive/<publication-id>/<publication-id>.pdf
archive/<publication-id>/<publication-id>.epub
archive/<publication-id>/<publication-id>.md
```

A new format appears on the card; a removed format disappears. Flat filenames are still
accepted as **Recovered catalog** entries, but the canonical layout links them to official
metadata.

## Install and import

1. Extract the portable ZIP. The top-level folder is
   `Field Manual Viewer Portable v0.3.0`.
2. For the official T.REX ARMS release, run:

   ```text
   Import-Field-Manual-Vault.bat "D:\Field Manual Vault\T.Rex Arms Field Manual Vault"
   ```

3. Start the viewer with `Start-Field-Manual-Viewer.bat`.
4. For a publisher-side direct download instead, use `Set-Up-Library.bat`.

**Downgrade warning:** Database compatibility with earlier releases has not been established.
Do not install v0.3.0 over an existing library and then run an older version against the same
`data/` folder. Keep older versions and their libraries separate.

## Verification

- Automated suite: 96 tests, 95 passed, 1 optional captured-source audit skipped.
- Vault import verified against the official `index.csv` SHA-256 values.
- Portable packaging contains no PDF, EPUB, Markdown manual, database or archive data.
- SHA-256: `6e720cd3813df343f9c73ac110f44a06fc855b0f17a38546c60db77e7d3f2537`
- Size: `31,590,883 bytes`

## Assets

- `Field-Manual-Viewer-Portable-v0.3.0-Windows-x64.zip`
- `Field-Manual-Viewer-Portable-v0.3.0-Windows-x64.zip.sha256`
- `Field-Manual-Viewer-Windows-x64.zip` (byte-identical compatibility alias)
- `Field-Manual-Viewer-Windows-x64.zip.sha256`
- `release-notes-v0.3.0.md`

v0.2.12 and the original v0.2.11 release remain available.
