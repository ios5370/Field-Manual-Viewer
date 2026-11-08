$ErrorActionPreference = "Stop"
$packageRoot = Split-Path -Parent $PSScriptRoot
Set-Location $packageRoot
$runtime = Join-Path $packageRoot "runtime\python"
$python = Join-Path $runtime "python.exe"
$ready = Join-Path $runtime ".setup-complete"
if (Test-Path -LiteralPath $ready) { exit 0 }
New-Item -ItemType Directory -Force -Path $runtime, (Join-Path $packageRoot "tmp") | Out-Null
$env:TEMP = Join-Path $packageRoot "tmp"
$env:TMP = $env:TEMP

# Pinned CPython embeddable runtime. The hash must match or preparation stops.
$pythonZip = Join-Path $packageRoot "tmp\python-embed.zip"
$pythonHash = "90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159"
Write-Host "Setting up the private Python runtime. Internet access is required."
if (-not (Test-Path -LiteralPath $python)) {
  Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.14.10/python-3.14.10-embed-amd64.zip" -OutFile $pythonZip -UseBasicParsing
  $actual = (Get-FileHash -LiteralPath $pythonZip -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($actual -ne $pythonHash) { throw "Python download hash mismatch: expected $pythonHash, got $actual." }
  Expand-Archive -LiteralPath $pythonZip -DestinationPath $runtime -Force
}

# Make the runtime import packages from Lib\site-packages and from the application root.
$pth = Get-ChildItem -LiteralPath $runtime -Filter "python*._pth" | Select-Object -First 1
if (-not $pth) { throw "Python runtime configuration not found." }
$content = Get-Content -LiteralPath $pth.FullName -Raw
$content = $content -replace '(?m)^#\s*import site\s*$', 'import site'
if ($content -notmatch '(?m)^\.\.\\\.\.$') { $content += [Environment]::NewLine + '..\..' + [Environment]::NewLine }
Set-Content -LiteralPath $pth.FullName -Value $content -Encoding ASCII

# Pinned pip wheel loaded directly from disk. This avoids executing an unpinned remote
# installer script. The PyPI file URL is immutable for a given release.
$pipWheel = Join-Path $packageRoot "tmp\pip-26.3-py3-none-any.whl"
$pipUrl = "https://files.pythonhosted.org/packages/07/25/ee8c38ae72d66b844ce8eb9711ba905dba21ca1a8a0c960057c41cd23337/pip-26.3-py3-none-any.whl"
$pipHash = "f3b60b20ffaadd4415ed7957212a38ac63fa4c5fa375bd0f4a797777940fad13"
if (-not (Test-Path -LiteralPath $pipWheel)) {
  Invoke-WebRequest -Uri $pipUrl -OutFile $pipWheel -UseBasicParsing
}
$pipActual = (Get-FileHash -LiteralPath $pipWheel -Algorithm SHA256).Hash.ToLowerInvariant()
if ($pipActual -ne $pipHash) { throw "pip wheel hash mismatch: expected $pipHash, got $pipActual. Delete tmp\pip-26.3-py3-none-any.whl and retry." }

$requirements = Join-Path $packageRoot "requirements.txt"
& $python -c "import sys;sys.path.insert(0,r'$pipWheel');from pip._internal.cli.main import main;sys.exit(main(['install','--no-cache-dir','--disable-pip-version-check','--no-warn-script-location','-r',r'$requirements']))"
if ($LASTEXITCODE -ne 0) { throw "Could not install required libraries." }
& $python -c "import bs4,json5,pypdf,pypdfium2,PIL,cryptography,field_manual_viewer"
if ($LASTEXITCODE -ne 0) { throw "Python dependency check failed." }
Set-Content -LiteralPath $ready -Value "Setup complete" -Encoding ASCII
Write-Host "Setup complete."
