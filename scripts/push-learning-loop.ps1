# ─────────────────────────────────────────────────────────────────────────────
# push-learning-loop.ps1
#
# Back up the Learning Loop failure reports to GitHub. Run this AFTER you close
# the UI — it commits every new/changed report in "Learning Loop/" and pushes
# to your current branch on origin. It touches ONLY the Learning Loop folder,
# so it never commits run-output binaries (models, STLs) from UI_Output/.
#
# Usage (from anywhere):
#   powershell -ExecutionPolicy Bypass -File "C:\Users\joeka\MTI_Claude\scripts\push-learning-loop.ps1"
# or, from the repo folder:
#   .\scripts\push-learning-loop.ps1
# ─────────────────────────────────────────────────────────────────────────────
$ErrorActionPreference = 'Stop'
# The script lives in scripts/; the Learning Loop folder is at the repo root.
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)

git add -- "Learning Loop"

$pending = git status --porcelain -- "Learning Loop"
if (-not $pending) {
    Write-Host "Learning Loop is already up to date - nothing to push." -ForegroundColor Yellow
    return
}

$count = ($pending | Measure-Object -Line).Lines
$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm'
git commit -m "chore(learning-loop): sync $count run report change(s) - $stamp"

$branch = (git rev-parse --abbrev-ref HEAD).Trim()
git push origin $branch

Write-Host "Pushed $count Learning Loop change(s) to origin/$branch." -ForegroundColor Green
