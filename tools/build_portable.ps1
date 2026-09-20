<#
Build a portable, no-install ZIP of the Plant Simulator for Windows.

    dist\PlantSimulator\           the folder to share (or the ZIP next to it)
        Plant Simulator.exe        double-click this (tray icon, no console window)
        Plant Simulator.bat        ...or this, if the exe is blocked - same thing with a console
        runtime\                   a private, bundled Python with every package installed
        app.py, plant_sim\, data\, .streamlit\, launch.py, README.md ...

Run it by double-clicking "tools\Build portable package.bat", or:
    powershell -ExecutionPolicy Bypass -File tools\build_portable.ps1

Needs internet on the build machine only (downloads the official python.org
embeddable runtime and the packages). The people you send the ZIP to need
nothing installed - they unzip it and double-click "Plant Simulator.bat".
#>

[CmdletBinding()]
param(
    [string]$PythonVersion = "3.11.9",
    [string]$OutDir = ""
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $OutDir) { $OutDir = Join-Path $root "dist" }
$stage = Join-Path $OutDir "PlantSimulator"
$runtime = Join-Path $stage "runtime"
$downloads = Join-Path $OutDir "downloads"

Write-Host "Building the portable Plant Simulator into $stage"
# A running copy locks its files - say so plainly instead of failing on a .pyd.
$running = Get-Process -Name "Plant Simulator" -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$stage*" }
if ($running) {
    throw "Plant Simulator.exe is running from $stage - stop it first (right-click its tray icon > Stop Plant Simulator), then build again."
}
# Clear the folder's contents rather than the folder itself: an Explorer
# window open on it would otherwise block the delete.
New-Item -ItemType Directory -Force $stage | Out-Null
Get-ChildItem -Force $stage | Remove-Item -Recurse -Force
New-Item -ItemType Directory -Force $downloads | Out-Null

# -- 1. the official embeddable Python (python.org) -------------------------
$short = ($PythonVersion -split "\.")[0..1] -join ""          # 3.11.9 -> 311
$zipName = "python-$PythonVersion-embed-amd64.zip"
$zipPath = Join-Path $downloads $zipName
if (-not (Test-Path $zipPath)) {
    $url = "https://www.python.org/ftp/python/$PythonVersion/$zipName"
    Write-Host "Downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $zipPath -UseBasicParsing
}
Write-Host "Unpacking the Python runtime"
Expand-Archive -Path $zipPath -DestinationPath $runtime -Force

# The embeddable build ignores site-packages until "import site" is enabled
# in its ._pth file - that is what lets pip-installed packages be found.
$pth = Join-Path $runtime "python$short._pth"
(Get-Content $pth) -replace "^#\s*import site", "import site" | Set-Content -Encoding ascii $pth
# ...and the embeddable build ONLY searches the paths in that file (not the
# current folder), so add the app folder (one level up) for "import plant_sim".
Add-Content -Encoding ascii $pth ".."

# -- 2. pip, then the app's packages ----------------------------------------
$getPip = Join-Path $downloads "get-pip.py"
if (-not (Test-Path $getPip)) {
    Write-Host "Downloading get-pip.py"
    Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getPip -UseBasicParsing
}
$python = Join-Path $runtime "python.exe"
Write-Host "Installing pip into the runtime"
& $python $getPip --no-warn-script-location --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { throw "pip bootstrap failed" }
Write-Host "Installing the simulator's packages"
& $python -m pip install --no-warn-script-location --disable-pip-version-check -r (Join-Path $root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "package install failed" }

# -- 3. the app itself --------------------------------------------------------
Write-Host "Copying the app"
& (Join-Path $root "tools/build_launcher.ps1") -OutDir $stage      # "Plant Simulator.exe"
Copy-Item (Join-Path $root "app.py") $stage
Copy-Item (Join-Path $root "launch.py") $stage
Copy-Item (Join-Path $root "Plant Simulator.bat") $stage
Copy-Item (Join-Path $root "requirements.txt") $stage
Copy-Item (Join-Path $root "README.md") $stage
Copy-Item (Join-Path $root "CALIBRATION_NOTES.md") $stage
Copy-Item (Join-Path $root "plant_sim") (Join-Path $stage "plant_sim") -Recurse
Get-ChildItem (Join-Path $stage "plant_sim") -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
New-Item -ItemType Directory -Force (Join-Path $stage ".streamlit") | Out-Null
Copy-Item (Join-Path $root ".streamlit\config.toml") (Join-Path $stage ".streamlit")
New-Item -ItemType Directory -Force (Join-Path $stage "data") | Out-Null
Copy-Item (Join-Path $root "data\staff_roster.csv") (Join-Path $stage "data")
$settings = Join-Path $root "data\app_settings.json"
if (Test-Path $settings) { Copy-Item $settings (Join-Path $stage "data") }   # ship the current sidebar settings too

# -- 4. smoke test: the bundled Python can import the app's modules ----------
Write-Host "Checking the bundle"
Push-Location $stage
try {
    & $python -c "import streamlit, pandas, altair; import plant_sim.simulation, plant_sim.config; print('bundle OK: streamlit', streamlit.__version__)"
    if ($LASTEXITCODE -ne 0) { throw "bundle check failed" }
} finally { Pop-Location }

# -- 5. zip it -----------------------------------------------------------------
$zipOut = Join-Path $OutDir ("PlantSimulator-" + (Get-Date -Format "yyyy-MM-dd") + ".zip")
if (Test-Path $zipOut) { Remove-Item -Force $zipOut }
Write-Host "Zipping to $zipOut (this takes a minute)"
Compress-Archive -Path $stage -DestinationPath $zipOut -CompressionLevel Optimal
$size = [math]::Round((Get-Item $zipOut).Length / 1MB)
Write-Host ""
Write-Host "Done. Share $zipOut ($size MB)."
Write-Host "Recipients unzip it anywhere and double-click 'Plant Simulator.exe'."
