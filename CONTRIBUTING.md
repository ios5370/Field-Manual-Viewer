# Contributing to Field Manual Viewer

Thanks for taking an interest. This is a small, deliberately conservative project: it acquires
a manual collection once and then works entirely offline. Changes are judged mainly on whether
they keep an ordinary, non-technical user safe and unpuzzled.

## Reporting a problem

Open an issue at <https://github.com/ios5370/Field-Manual-Viewer-Clean/issues> and include:

- the application version (shown in Library Setup and by `/api/health` on the local viewer);
- **which** folder the launcher reported it manages, described in a sanitized form. Keep the
  drive letter and the folder name, but replace your Windows user name with `<user>`, for
  example `D:\Manuals\Field-Manual-Viewer`. Each extracted folder is an independent library, so
  distinguishing the folder matters; publishing exactly where it sits on your disk does not;
- which launcher you used (`Set-Up-Library.bat` or `Start-Field-Manual-Viewer.bat`);
- what you expected, what happened, and the relevant entries from `reports/`;
- whether the problem involves downloads, indexing, reading or the viewer itself.

Before posting, redact:

- your Windows user name, home directory and any full path that contains them;
- other personal or machine names, account identifiers and email addresses;
- anything from `data/` or `archive/` that identifies your own files.

Do not attach original manuals, populated `data/` databases, or a whole `reports/` folder. A
short description plus the relevant sanitized log excerpt is enough.

If the report describes behaviour that differs from another running copy of the application,
say which copy answered. "Which folder and process responded" has been the source of past
confusion, so it is treated as part of the bug.

## Proposing a change

1. Read `BUILDING.md` and prepare the runtime.
2. Describe the problem the change solves before writing much code.
3. Keep the change small and reviewable. This project favours a narrow correction over a
   refactor.
4. Add or update a focused test. The suite is offline and self-contained, so new tests must
   not depend on the live publisher, a private archive or a specific machine.
5. Run the relevant test modules while iterating, then the full suite once before proposing
   the change.

```powershell
runtime\python\python.exe -m unittest discover -s tests -v
```

## Boundaries

These are product decisions, not preferences. Changes that cross them need an explicit
discussion first.

- **No telemetry, accounts, cloud services or AI dependencies.** The library must keep working
  with the publisher offline and the project abandoned.
- **No update service and no silent refresh.** Reopening the application must not start a
  download or change a finished archive.
- **Local only.** The viewer binds to the loopback interface and must not become reachable from
  the network.
- **Counting contract.** Publications, format files and the 20-publication test batch are
  distinct units. Do not merge them into one number.
- **Per-folder isolation.** Each extracted folder is its own library. Never stop, reuse or
  count another folder's viewer.
- **No regression in safe pause and resume.** Verified originals must never be re-downloaded.
- **Licensing and credit.** Keep the MIT licence, the T.REX ARMS credit, the explicit
  non-affiliation statement and the third-party notices intact.

## Code style

Match the surrounding code. The application intentionally avoids frameworks: plain Python,
plain HTML/CSS/JavaScript, and a small PowerShell GUI. Prefer readable `.py`, `.ps1`, `.bat`,
`.js`, `.html` and `.css` sources over generated or opaque artefacts, because users are meant
to be able to inspect what runs on their machine.

## Licence of contributions

By contributing you agree that your contribution is licensed under the repository's
[MIT License](LICENSE). Do not contribute manuals, publisher content or third-party code whose
licence you cannot honour.
