# Field Manual Viewer

**Download field manuals once. Search and read them offline whenever you need them.**

Field Manual Viewer is a portable application for **64-bit Windows**. It saves original PDF, EPUB and Markdown publications on your computer, provides a searchable library in your regular web browser, and requires no account, AI service, or Python installation.

## Download for Windows

### [⬇ Download Field Manual Viewer Portable v0.3.0 — Windows ZIP](https://github.com/ios5370/Field-Manual-Viewer/releases/download/v0.3.0/Field-Manual-Viewer-Portable-v0.3.0-Windows-x64.zip)

[Verify the v0.3.0 SHA-256 checksum](https://github.com/ios5370/Field-Manual-Viewer/releases/download/v0.3.0/Field-Manual-Viewer-Portable-v0.3.0-Windows-x64.zip.sha256)

<sub>[Previous version — v0.2.12](https://github.com/ios5370/Field-Manual-Viewer/releases/download/v0.2.12/Field-Manual-Viewer-Portable-v0.2.12-Windows-x64.zip)</sub>

<sub>[Original release — v0.2.11](https://github.com/ios5370/Field-Manual-Viewer/releases/download/v0.2.11/Field-Manual-Viewer-Windows-x64.zip)</sub>

<sub>v0.2.12 and v0.2.11 lack Markdown reading and Field Manual Vault import. Do not downgrade an existing v0.3.0 installation in place; database compatibility with earlier versions has not been established.</sub>

> **The Windows ZIP includes the portable runtime and no manuals.** The v0.3.0 asset extracts to **Field Manual Viewer Portable v0.3.0**. The application source is available in this repository for inspection.

## Getting started

![Four illustrated steps: download the Windows ZIP, extract it, run Set-Up-Library, then open the viewer](docs/getting-started.svg)

1. **Download Field Manual Viewer Portable v0.3.0** using the link above.
2. In your Downloads folder, **right-click the ZIP** and choose **Extract All**. Choose a writable location with plenty of space, such as a folder on an external drive. The extracted folder is **Field Manual Viewer Portable v0.3.0**.
3. Open the extracted **Field Manual Viewer Portable v0.3.0** folder. For the T.REX ARMS release, double-click **Import-Field-Manual-Vault.bat** and select the extracted vault folder. For a direct publisher download, use **Set-Up-Library.bat** instead.
4. To browse and search, double-click **Start-Field-Manual-Viewer.bat** (or **Start-Field-Manual-Viewer**). The library opens in your usual browser.

**Space needed:** The full 2026-11 reference collection is about **110 GB** as PDF + EPUB + Markdown. We recommend **about 140 GB of free space** before beginning. If you already have an earlier library, keep it and import only the delta; a full clean collection still needs the full space.

**Need to stop?** Closing Library Setup requests a safe pause. Reopen it later and select **Resume Download**. Previously verified files are retained.

**Once downloaded, the library works offline.** You can search, view and read saved manuals even if the original website becomes unavailable. Keep the entire extracted folder together and, if possible, copy it to a second drive as a backup.

## Markdown and Field Manual Vault import

v0.3.0 adds a native **Markdown** reader and search index alongside PDF and EPUB.

If you downloaded the official T.REX ARMS `FieldManualVault_2026-11` torrent, keep it extracted somewhere with enough room, then run:

```text
Import-Field-Manual-Vault.bat "D:\Field Manual Vault\T.Rex Arms Field Manual Vault"
```

The importer reads the release `index.csv`, opens the per-topic ZIPs, verifies every file against its published SHA-256, and writes a clean portable library under `archive/<publication-id>/`. Existing files with the same checksum are skipped, so re-running it resumes or updates without downloading anything again. The source vault is not modified.

The portable ZIP itself contains **no manuals**. The library is created by the import.

## Adding or removing individual files

The viewer watches `archive/` and updates cards automatically.

- To add a format or a new publication, put the file in `archive/<publication-id>/<publication-id>.<pdf|epub|md>`.
- A format added to an existing publication appears on its card.
- Removing a file makes that format disappear from the card without deleting the catalog metadata.
- Files copied into a flat folder are still accepted as **Recovered catalog** entries, but the canonical `archive/<id>/<id>.<format>` layout keeps them linked to the official metadata.

## Moving or recovering an existing library

The original `archive/` folder is the part you must preserve. It contains the PDF, EPUB and Markdown files themselves.

- **Trying a new version:** extract the new release into a separate version-named folder (for example `Field Manual Viewer Portable v0.3.0`) and move your existing `archive/` folder into it. A move on the same drive is instant and does not duplicate the documents. You can move `data/` as well to keep the original catalog metadata and search indexes, but the viewer can rebuild both offline from the archive.
- **Upgrading in place without moving the archive:** close the viewer and Library Setup. Extract the new release to a temporary folder, then copy only the new application files and `runtime/` over the old installation. Leave the existing `archive/` and `data/` folders untouched.
- **Only the archive survived:** put it in the correct place, open the viewer, and wait while it checks the files and rebuilds the catalog and search index locally. Nothing is downloaded.
- **Adding or removing files while the viewer is open:** choose **Rescan library** in the viewer, or refresh the browser page.
- **Recovered entries** are labelled **Recovered catalog**. Their titles may come from the filename, the document's embedded metadata, or a surviving manifest rather than the publisher catalog.
- **Do not use Download All to recover an existing archive.** Recovery is automatic; Download All is only for acquiring publications that are not already present.

The first recovery of a large copied archive reads each new or changed file once to calculate its checksum and build its search index. Later launches use the recorded checksums and are fast.

## How startup and checking work

- A normal launch shows the saved catalog immediately. A lightweight archive check then runs in the background and updates the library when files were added, removed, changed, or restored.
- An archive-only recovery cannot show unknown documents instantly. Recovered publications appear progressively while the catalog and search index are rebuilt.
- **Rescan library** requests a quick filesystem check. For a full checksum verification, close the viewer and use **Library Setup > Advanced > Check saved file integrity**.
- Browser filters and searches use the saved catalog and do not start repeated archive scans. Refreshing the page requests one throttled background check; multiple tabs share it.
- If a document disappears or changes, its catalog record and index are retained. While the background check catches up, a card may briefly remain visible, but search results and the reader refuse stale content.
- If the whole archive drive is disconnected, the last known catalog remains visible with an explicit warning. Reconnect the drive; no records are destroyed.

## A few useful details

- **No manuals are included in the ZIP.** The setup program or the Field Manual Vault importer creates your library from originals you obtain separately.
- **Markdown is a first-class format.** When an official `.md` file is present, the card offers **Read MD** alongside PDF and EPUB, and its text participates in local search.
- **No regular updates are needed.** Library Setup is primarily a one-time tool; you will normally use the browser viewer afterward.
- **Nothing to install system-wide.** The release contains the application and its Windows runtime.
- **The folder is portable.** Close the viewer and Library Setup before copying or moving the whole folder to another drive. All library paths are relative, and the launcher replaces stale saved server information automatically.
- **Windows security prompts:** Only download from the Releases link on this page. The application is not code-signed; Windows may display an unfamiliar-app warning. Do not disable your security software.

## Credits and license

Thanks to [T.REX ARMS Field Manual Vault](https://www.trex-arms.com/field-manual-vault) for collecting and organizing the source publications. **Field Manual Viewer is an independent project, not affiliated with or endorsed by T.REX ARMS.** Each publication retains its own rights and terms.

Application code: [MIT License](LICENSE). Third-party software and license notices are included with the portable package.


## Source and building

Application code is [MIT licensed](LICENSE) and fully public, including the automated tests and build scripts.

- [BUILDING.md](BUILDING.md) — prepare a development environment, run the tests and build the Windows ZIP.
- [CONTRIBUTING.md](CONTRIBUTING.md) — how to report a problem or propose a change.
- [THIRD-PARTY.md](THIRD-PARTY.md) — runtime components and their upstream sources.
- [VALIDATION.md](VALIDATION.md) — what has been verified and what has not.

A source checkout is not the user download: it contains no Python runtime and no manuals. Ordinary users should use the Windows ZIP linked above.

