# Local check: linter, tests, dependency audit, shellcheck of the backup scripts.
# Requires a running DB: docker compose up -d db (the host port comes from DB_HOST_PORT, default 5433).
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not $env:DB_HOST_PORT) { $env:DB_HOST_PORT = "5433" }
if (-not $env:TEST_DATABASE_URL) {
    $env:TEST_DATABASE_URL = "postgresql+asyncpg://aichat:aichat@127.0.0.1:$($env:DB_HOST_PORT)/aichat_test"
}
$py = ".venv\Scripts\python.exe"

& $py -m ruff check .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $py -m pytest -q -W error
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $py -m pip_audit
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# shellcheck in a pinned image (it has no shell, so the file list is built here).
# docker output (including stderr while pulling the image) goes straight to the console: in PowerShell 5.1
# redirecting an external program's stderr with $ErrorActionPreference = "Stop" kills the script.
# The result is determined only by the exit code.
$shellcheckImage = "koalaman/shellcheck:v0.11.0"
$scripts = @(Get-ChildItem ops/backup/*.sh | ForEach-Object { "ops/backup/$($_.Name)" }) + "ops/backup/test/run.sh"
$ErrorActionPreference = "Continue"
docker run --rm -v "${PWD}:/mnt" -w /mnt $shellcheckImage @scripts
$shellcheckExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($shellcheckExit -ne 0) { exit $shellcheckExit }

Write-Host "All checks passed."
