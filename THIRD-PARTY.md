# Runtime components

The portable Windows package includes Python 3.14.10 (Windows x64 embeddable distribution) from python.org. Its embeddable runtime ZIP was checked against SHA-256 `90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159`.

The Python license is retained in `runtime/python/LICENSE.txt`. Production Python libraries are listed in `requirements.txt`. Package metadata and license files remain in their `runtime/python/Lib/site-packages/*dist-info` directories, including licenses for PDFium, Pillow and cryptography dependencies. These components retain their own licenses, separately from the application's MIT license.

The app uses the existing Windows default browser. No browser binary, PdfCraft bundle, downloaded manual or populated library database is included in the portable release. Publications acquired by the user retain their source licenses and terms.


## Upstream source

Application source is in this repository. Compiled dependencies keep their own licenses and public upstream projects:

- CPython: https://github.com/python/cpython
- PDFium: https://pdfium.googlesource.com/pdfium/
- pypdfium2: https://github.com/pypdfium2-team/pypdfium2
- pypdf: https://github.com/py-pdf/pypdf
- Pillow: https://github.com/python-pillow/Pillow
- cryptography: https://github.com/pyca/cryptography
- fontTools: https://github.com/fonttools/fonttools
- Beautiful Soup: https://www.crummy.com/software/BeautifulSoup/
- json5: https://pypi.org/project/json5/

The exact direct versions are pinned in requirements.txt; installed package metadata and licenses identify transitive components in the portable runtime. Build scripts are inspectable. The Python embeddable ZIP and the pip wheel used during preparation are verified against SHA-256 pins recorded in `tools/Bootstrap-Runtime.ps1`. Byte-for-byte reproducible dependency builds are not currently claimed.
