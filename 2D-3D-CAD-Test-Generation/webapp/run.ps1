# Launch the MTI 2D->3D pipeline web UI on Windows (same fixed UI everywhere).
#   .\run.ps1              # http://127.0.0.1:8092
#   $env:PORT=9000; .\run.ps1
# First run creates .venv and installs the PINNED dependency set; later runs
# start in seconds. Requires Python 3.10+ on PATH (winget install Python.Python.3.12).
# NOTE: this file must stay pure ASCII - Windows PowerShell 5.1 parses BOM-less
# .ps1 files as ANSI and non-ASCII characters break it.
$ErrorActionPreference = "Stop"

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Project = Split-Path -Parent $Here
$Port = if ($env:PORT) { $env:PORT } else { "8092" }
$Venv = Join-Path $Here ".venv"
$Py = Join-Path $Venv "Scripts\python.exe"

if (-not (Test-Path $Py)) {
    Write-Host "Creating venv at $Venv ..."
    python -m venv $Venv
    if (-not (Test-Path $Py)) { throw "Could not create the venv - is Python 3.10+ installed and on PATH?" }
    Write-Host "Installing pinned dependencies (pipeline + UI) ..."
    & $Py -m pip install -q --upgrade pip
    & $Py -m pip install -q -r (Join-Path $Project "requirements.txt")
    & $Py -m pip install -q -r (Join-Path $Here "requirements-ui.txt")
}

Write-Host ""
Write-Host "  MTI 2D->3D Pipeline UI  ->  http://127.0.0.1:$Port"
Write-Host "  (Ctrl+C to stop)"
Write-Host ""
Start-Process "http://127.0.0.1:$Port/"
& $Py -m uvicorn app:app --app-dir $Here --host 127.0.0.1 --port $Port
