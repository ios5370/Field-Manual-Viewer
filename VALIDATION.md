# Release validation

Qualified on Windows x64. These results describe observed behaviour and its scope. They are
not a guarantee that every Windows configuration has been tested, and they are not an
independent security audit.

## Automated checks

The v0.2.12 suite contains 91 tests: 90 pass and one optional captured-source audit is skipped,
because that audit needs source captures that are not distributed in a clean checkout. The
suite is offline and self-contained; it builds its own temporary libraries and never reads a
real archive. The v0.2.12 tests add archive-only and database-loss recovery, cached-first
catalog behavior, EPUB container hardening, and the indexed-lookup regression coverage.

Coverage includes:

- acquisition authorization, duplicate starts and storage guards;
- source pacing, backoff and cancellation;
- interrupted and range-resumed transfers, and structural PDF/EPUB validation;
- persistent test selection and no-repeat test-to-full transitions;
- concurrent catalog reading, cached-page recovery and metadata precedence;
- cached-first browsing while archive verification runs in the background;
- archive-only and database-loss recovery, including last-known-good records;
- original-file evidence, index readiness and direct PDF-page lookup;
- indexed-lookup maintenance without full FTS table scans;
- local HTTP boundaries, path-traversal rejection and per-folder isolation;
- windowless logging, viewer lifetimes and tab/transfer tracking;
- safe Library Setup close and start races, Windows sharing conflicts and default-browser
  failures.

## Acquisition and resume

A clean portable extraction completed live catalog reading and acquired 20 representative
publications as PDF and EPUB. All 40 originals passed validation and were indexed.

Controlled sources exercise real transfer validation and text extraction. Test, repeated test
and then full acquisition each fetch every format URL once and preserve saved hashes, lengths
and timestamps. Closing during catalog reading preserves progress. Resume indexes previously
downloaded selected files as well as newly acquired ones.

Actual Windows Forms acceptance showed the test files ready while metadata remained incomplete.
Close paused both activities and waited for worker exit. Resume finished metadata and the
remaining controlled publication without repeating the initial 40 files, with external sockets
blocked.

GUI smoke invokes the real close handler programmatically; it is not a physical mouse or
Alt-F4 test on another PC. Forced termination and power loss bypass that handler; durable
transfer state and SQLite transactions support later recovery.

## Offline readers, search and filters

Real-sample browser acceptance covers title and identifier search, deep PDF phrase hits,
rendering and page navigation, EPUB text and chapter navigation, related editions, filters
retained after refresh, dates and sorting, and mobile layout. Outside browser requests were
blocked and no JavaScript errors occurred.

A complete reference library was also exercised: availability, branch/type/subject/collection
and date filters, both readers, and existing full-text indexes. Joint-service publications
legitimately match their listed service branches.

A direct PDF-page map removes a slow lookup through unindexed full-text publication and page
fields. Existing text is backfilled into that map once, without re-extracting any PDF, and new
or rebuilt PDF indexes maintain it in the same transaction as their text.

## Fresh-folder isolation and shutdown

An empty extraction launched beside an existing 20-publication library on a different local
port and showed zero publications. Copying the existing library's saved server information and
old lock file into it did not redirect it or change the other library.

Opening the viewer or setup started no catalog reading and no downloads. Closing the fresh
viewer left the other library running. The closed extraction was moved, launched from its new
path, closed and deleted normally.

The actual Setup batch/windowless-wrapper/GUI path completed a safe close, and its fixture then
deleted normally. Viewer tests cover more than six tabs, background reading, reloads,
original-PDF wrappers, last-tab and whole-browser close, and explicit **Close Viewer**.

## Browser compatibility

The application uses the system default browser; no specific browser is required. Tests verify
successful dispatch and an actionable error when no browser handler can be opened. That failure
stops a newly started local service and releases its state and lock instead of leaving an
unused hidden viewer behind.

## Packaging and known limits

The release ZIP contains the readable application sources, the launchers, the local browser
interface, the pinned Python runtime, production dependencies and licence notices. The ZIP
extracts to a versioned top-level folder, e.g. `Field Manual Viewer Portable v0.2.12`; the
version is derived from `APP_VERSION` at build time. A byte-identical compatibility ZIP named
`Field-Manual-Viewer-Windows-x64.zip` preserves the README's stable “latest release” link. It
excludes the automated tests, the build tools, the developer-only acceptance and metadata-repair
modules, acquired originals, populated databases, search indexes, browser profiles and logs.

Known limits:

- The source PDF for one publication (`md_md0160_undated`) fails the PDF end-of-file check and
  is treated as a source exception. Its EPUB is retained. It is never counted as verified and
  is not retried indefinitely.
- Files added to `archive/` by hand are not imported or indexed automatically.
- Broad full-text searches remain disk and cache dependent; the SQL query has a 20-second
  budget and then asks for a more specific phrase.
- Scans without embedded text are not OCRed.
- Database compatibility between v0.2.12 and v0.2.11 has not been established. Do not install
  v0.2.12 in place and then downgrade to v0.2.11 against the same `data/` folder; keep a
  separate copy of the older version and library if a fallback is needed.
- A physical second PC, physical USB-drive moves, every DPI setting, enterprise policy and
  antivirus combination have not all been tested.
- The bundled runtime is third-party compiled software with public upstream sources and
  licences. Publishing source and a checksum is not a proof of byte-for-byte reproducible
  dependency builds.
- The application does not control independent background networking performed by the user's
  browser or by Windows.
