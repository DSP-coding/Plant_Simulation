<#
Compile "Plant Simulator.exe" - the native double-click launcher for the
portable build - from tools\launcher\PlantSimulator.cs, using the C#
compiler that ships with Windows (.NET Framework 4, present on every
Windows 10/11 PC). No SDK, no Visual Studio.

    powershell -ExecutionPolicy Bypass -File tools\build_launcher.ps1 [-OutDir <folder>]

Writes <OutDir>\Plant Simulator.exe (default: dist\). build_portable.ps1
calls this and drops the exe into the portable folder.
#>

[CmdletBinding()]
param([string]$OutDir = "")

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $OutDir) { $OutDir = Join-Path $root "dist" }
New-Item -ItemType Directory -Force $OutDir | Out-Null

$csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) { $csc = Join-Path $env:WINDIR "Microsoft.NET\Framework\v4.0.30319\csc.exe" }
if (-not (Test-Path $csc)) { throw "The .NET Framework 4 C# compiler (csc.exe) was not found - it ships with Windows 10/11." }

$src = Join-Path $root "tools\launcher\PlantSimulator.cs"
$ico = Join-Path $root "tools\plant_simulator.ico"
$exe = Join-Path $OutDir "Plant Simulator.exe"

Write-Host "Compiling $exe"
& $csc /nologo /target:winexe /optimize+ /out:"$exe" /win32icon:"$ico" `
    /r:System.dll /r:System.Drawing.dll /r:System.Windows.Forms.dll "$src"
if ($LASTEXITCODE -ne 0) { throw "csc failed" }
Write-Host "OK: $exe ($([math]::Round((Get-Item $exe).Length / 1KB)) KB)"
