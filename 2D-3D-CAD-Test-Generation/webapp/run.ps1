# Launch the MTI 2D->3D pipeline web UI on Windows (same fixed UI everywhere).
#   .\run.ps1              # http://127.0.0.1:8092
#   $env:PORT=9000; .\run.ps1
# First run picks a compatible Python, auto-installing Python 3.12 via winget if
# none is present, then creates .venv and installs the PINNED dependency set
# (pipeline + UI + DWG/DXF intake). Later runs start in seconds.
#
# Python is HARD-PINNED to >=3.10,<3.13. The CadQuery pre-validation stage pulls
# in numba, which has NO wheel for Python 3.13+; on 3.13 the dependency install
# aborts halfway and leaves rich/anthropic/etc. uninstalled (this exact bug bit
# us once). Keep this window in sync with setup.py's check_python().
#
# NOTE: this file must stay pure ASCII - Windows PowerShell 5.1 parses BOM-less
# .ps1 files as ANSI and non-ASCII characters break it.
$ErrorActionPreference = "Stop"

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Project = Split-Path -Parent $Here
$Port = if ($env:PORT) { $env:PORT } else { "8092" }
$Venv = Join-Path $Here ".venv"
$Py = Join-Path $Venv "Scripts\python.exe"

# --- Python version policy (keep in sync with setup.py) ---
$PyMin = [Version]"3.10"
$PyMaxExcl = [Version]"3.13"       # exclusive: 3.13+ has no numba wheel
$WingetId = "Python.Python.3.12"

function Test-PyCompatible($exe) {
    # $true only if $exe is a working python in [PyMin, PyMaxExcl).
    if (-not $exe) { return $false }
    try { $v = & $exe -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null }
    catch { return $false }
    if (-not $v) { return $false }
    try { $ver = [Version]$v } catch { return $false }
    return ($ver -ge $PyMin -and $ver -lt $PyMaxExcl)
}

function Find-Python {
    # 1) Prefer exact versions via the py launcher (newest compatible first).
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($tag in @("3.12", "3.11", "3.10")) {
            $p = (& py "-$tag" -c "import sys;print(sys.executable)" 2>$null)
            if ($LASTEXITCODE -eq 0 -and (Test-PyCompatible $p)) { return $p }
        }
    }
    # 2) Fall back to whatever 'python'/'python3' resolves to, if in range.
    foreach ($name in @("python", "python3")) {
        $c = Get-Command $name -ErrorAction SilentlyContinue
        if ($c -and (Test-PyCompatible $c.Source)) { return $c.Source }
    }
    return $null
}

if (-not (Test-Path $Py)) {
    $BasePy = Find-Python
    if (-not $BasePy) {
        Write-Host "No compatible Python (>=3.10,<3.13) found."
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            Write-Host "Installing $WingetId via winget (one-time) ..."
            winget install --id $WingetId -e --source winget `
                --accept-package-agreements --accept-source-agreements
            $BasePy = Find-Python   # py launcher registers the new interpreter
        }
    }
    if (-not $BasePy) {
        throw "No compatible Python (>=3.10,<3.13) and could not auto-install. Run:  winget install $WingetId"
    }
    Write-Host "Using base Python: $BasePy"
    Write-Host "Creating venv at $Venv ..."
    & $BasePy -m venv $Venv
    if (-not (Test-Path $Py)) { throw "Could not create the venv with $BasePy" }

    Write-Host "Installing pinned dependencies (pipeline + UI + DWG intake) ..."
    & $Py -m pip install --upgrade pip
    & $Py -m pip install -r (Join-Path $Project "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        Remove-Item -Recurse -Force $Venv
        throw "pip install (pipeline deps) failed - venv removed so the next run retries from clean. See the error above."
    }
    & $Py -m pip install -r (Join-Path $Here "requirements-ui.txt")
    if ($LASTEXITCODE -ne 0) {
        Remove-Item -Recurse -Force $Venv
        throw "pip install (UI deps) failed - venv removed so the next run retries from clean. See the error above."
    }
}

Write-Host ""
Write-Host "  MTI 2D->3D Pipeline UI  ->  http://127.0.0.1:$Port"
Write-Host "  (Ctrl+C to stop)"
Write-Host ""
Start-Process "http://127.0.0.1:$Port/"
& $Py -m uvicorn app:app --app-dir $Here --host 127.0.0.1 --port $Port
