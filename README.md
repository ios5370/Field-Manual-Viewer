# Field Manual Viewer

**Download field manuals once. Search and read them offline whenever you need them.**

Field Manual Viewer is a portable application for **64-bit Windows**. It saves original PDF and EPUB publications on your computer, provides a searchable library in your regular web browser, and requires no account, AI service, or Python installation.

## Download for Windows

### [⬇ Download Field Manual Viewer — Windows ZIP](https://github.com/ios5370/Field-Manual-Viewer/releases/latest/download/Field-Manual-Viewer-Windows-x64.zip)

> **The Windows ZIP includes the portable runtime.** The link above always points to the newest release. The application source is available in this repository for inspection.

## Getting started

![Four illustrated steps: download the Windows ZIP, extract it, run Set-Up-Library, then open the viewer](docs/getting-started.svg)

1. **Download** the Windows ZIP using the link above.
2. In your Downloads folder, **right-click the ZIP** and choose **Extract All**. Choose a writable location with plenty of space, such as a folder on an external drive.
3. Open the extracted **Field-Manual-Viewer** folder and double-click **Set-Up-Library.bat** (Windows may show it simply as **Set-Up-Library**). Select **Download All**. You can instead try **Download Test Batch (20)** first.
4. To browse and search, double-click **Start-Field-Manual-Viewer.bat** (or **Start-Field-Manual-Viewer**). The library opens in your usual browser.

**Space needed:** The reference collection occupies roughly **70 GB** once downloaded. We recommend **about 100 GB of free space** before beginning. Downloading may take several hours. Keep the computer awake and connected to the internet.

**Need to stop?** Closing Library Setup requests a safe pause. Reopen it later and select **Resume Download**. Previously verified files are retained.

**Once downloaded, the library works offline.** You can search, view and read saved manuals even if the original website becomes unavailable. Keep the entire extracted folder together and, if possible, copy it to a second drive as a backup.

## A few useful details

- **No manuals are included in the ZIP.** The setup program retrieves them from the original public source while that source is available.
- **No regular updates are needed.** Library Setup is primarily a one-time tool; you will normally use the browser viewer afterward.
- **Nothing to install system-wide.** The release contains the application and its Windows runtime.
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

