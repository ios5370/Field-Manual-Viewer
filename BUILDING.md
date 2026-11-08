# Building Field Manual Viewer

This document describes how to reproduce the portable Windows application from this
repository. It covers the supported platform, preparing a local Python runtime, running the
tests, and building and verifying the release ZIP.

If you only want to *use* the application, download the Windows ZIP from the
[latest release](https://github.com/ios5370/Field-Manual-Viewer/releases/latest) instead. A
source checkout contains no runtime, no manuals and no prepared library.

## Supported platform

- Windows 10 or Windows 11, x64.
- PowerShell 5.1 (included with Windows) or later.
- Internet access the first time you prepare the runtime.
- About 1 GB of free space for the runtime, dependencies and build output. A real library
  needs far more; see the README.

There are currently no macOS or Linux launchers.

## Source layout

| Path | Purpose |
|---|---|
| `field_manual_viewer/` | Application package: acquisition, catalog, indexing, readers, local server, lifecycle |
| `field_manual_viewer/static/` | Browser interface (plain HTML, CSS and JavaScript, no CDN) |
| `Library-Setup.ps1` | Windows Forms Library Setup window |
| `Set-Up-Library.bat`, `Start-Field-Manual-Viewer.bat` | User-facing launchers |
| `tests/` | Automated regression suite and optional browser acceptance scripts |
| `tools/Bootstrap-Runtime.ps1` | Prepares the pinned Python runtime and dependencies |
| `tools/build_portable.py` | Builds the portable release ZIP from an explicit manifest |
| `requirements.txt` | Pinned direct Python dependencies |

## 1. Get the source

```powershell
git clone https://github.com/ios5370/Field-Manual-Viewer.git
cd Field-Manual-Viewer
```

No private repository, credential or internal service is required.

## 2. Prepare the runtime

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/Bootstrap-Runtime.ps1
```

The script creates `runtime/python/`, which is ignored by Git. It downloads exactly two
artefacts and verifies each against a SHA-256 pin recorded in the script:

| Artefact | Source | Pin |
|---|---|---|
| CPython 3.14.10 embeddable amd64 | `python.org` | `90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159` |
| pip 26.3 wheel | `files.pythonhosted.org` (immutable release URL) | `f3b60b20ffaadd4415ed7957212a38ac63fa4c5fa375bd0f4a797777940fad13` |

The pip wheel is loaded directly from the downloaded file, so the build never executes an
unverified remote script. It then installs the pinned packages from `requirements.txt` and
checks that each one imports.

The script is idempotent. Delete `runtime/python/.setup-complete` to rebuild, or remove the
whole `runtime/` folder to start clean.

## 3. Run it from source

The application resolves all library paths relative to the repository root, so a source
checkout behaves like an installed copy.

```powershell
# Local viewer on the default port; opens your browser
runtime\python\python.exe -m field_manual_viewer serve --port 8765

# Catalog status as JSON
runtime\python\python.exe -m field_manual_viewer report

# Explicit, bounded acquisition of a chosen publication
runtime\python\python.exe -m field_manual_viewer collect --id <publication-id>
```

Do not start a full acquisition to explore the code. `collect` with an explicit `--id` is
bounded, and the test suite uses local fixtures rather than the live publisher.

## 4. Run the tests

```powershell
runtime\python\python.exe -m unittest discover -s tests -v
```

The suite is self-contained and offline. It creates its own temporary libraries and does not
read or write any real archive. Current expected result on Windows x64 is 81 tests, one
optional captured-source audit skipped, because that audit needs private source captures that
are not distributed.

Useful focused subsets while developing:

```powershell
runtime\python\python.exe -m unittest tests.test_launcher -v
runtime\python\python.exe -m unittest tests.test_test_batch_flow -v
runtime\python\python.exe -m unittest tests.test_lifecycle tests.test_reader_lookup -v
```

The two acceptance scripts in `tests/` drive a real browser and require Playwright plus a
developer Chromium build. They are optional and are not part of the normal suite:

```powershell
runtime\python\python.exe tests/portable_browser_acceptance.py --library-root "<isolated-library>" --browser-executable "<chrome.exe>" --port <port>
runtime\python\python.exe tests/windowless_viewer_acceptance.py --library-root "<closed-isolated-library>" --browser-executable "<chrome.exe>"
```

Never point those scripts at an irreplaceable archive: the portable acceptance test
temporarily moves a fixture PDF to check missing-file behaviour.

## 5. Build the portable ZIP

```powershell
runtime\python\python.exe tools/build_portable.py
```

Output:

- `build/Field-Manual-Viewer-Windows-x64.zip`
- `build/Field-Manual-Viewer-Windows-x64.zip.sha256`

The builder packages an **explicit manifest**, not everything in the checkout. It writes a
single top-level `Field-Manual-Viewer/` folder containing:

- `field_manual_viewer/` application modules and `static/` browser resources;
- both `.bat` launchers and `Library-Setup.ps1`;
- `README.md`, `LICENSE`, `THIRD-PARTY.md`;
- the prepared `runtime/python/` tree, including its license files and package metadata.

It refuses to run without a prepared runtime, and it refuses to package `data/`, `archive/`,
`reports/`, `tmp/` or `.git/`.

Deliberately **not** in the ZIP: `tests/`, `tools/`, `VALIDATION.md`, `BUILDING.md`,
`CONTRIBUTING.md`, `requirements.txt`, `docs/`, `.gitignore`, and the developer-only modules
`field_manual_viewer/acceptance.py` and `field_manual_viewer/repair_metadata.py`. Those remain
public in the repository so that behaviour can be reproduced and audited; they are simply not
shipped to people who only want to read manuals.

## 6. Verify the build

The builder already asserts the archive has no PDF, EPUB or SQLite entries, no library data
directories, and that the compressed stream is intact. For a release, also check the bytes:

```powershell
# Recorded checksum
Get-Content build\Field-Manual-Viewer-Windows-x64.zip.sha256

# Independent verification
(Get-FileHash build\Field-Manual-Viewer-Windows-x64.zip -Algorithm SHA256).Hash.ToLowerInvariant()

# Inspect the actual entry list
Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::OpenRead("$PWD\build\Field-Manual-Viewer-Windows-x64.zip").Entries |
    ForEach-Object { $_.FullName } | Sort-Object
```

Extract the ZIP into an empty folder with a space in its path, run
`Set-Up-Library.bat`, confirm it reports the correct folder, and confirm that opening the
viewer does not start any download. Then confirm that installing this build over an existing
library leaves `archive/` and `data/` untouched.

## Dependencies and provenance

Direct versions are pinned in `requirements.txt` and are installed from PyPI. Transitive
dependencies are resolved by pip at build time and are not pinned. `THIRD-PARTY.md` lists the
components and their upstream projects; installed package metadata and licence files are
shipped inside the ZIP under `runtime/python/Lib/site-packages/*.dist-info`.

Builds are **not** currently claimed to be byte-for-byte reproducible: the Python embeddable
ZIP and the pip-resolved transitive packages are mutable inputs, and ZIP timestamps vary.
The application source itself is pinned by commit.

## Preparing a release

1. Update `APP_VERSION` in `field_manual_viewer/__init__.py` and the version shown in
   `README.md`.
2. Run the full test suite.
3. Build the ZIP and record its SHA-256.
4. Confirm every application file in the ZIP matches the tagged source commit.
5. Publish a **new** version tag and release. Do not replace the asset of an existing release
   with different bytes.
