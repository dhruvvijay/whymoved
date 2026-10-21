# Start WhyMoved on Windows. Right-click > Run with PowerShell, or from a terminal in this folder:
#   powershell -ExecutionPolicy Bypass -File .\START_HERE.ps1
# First run creates a virtual environment and installs what is needed (about a minute).
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    python -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install -q -r requirements-api.txt
Write-Host ""
Write-Host "Open http://127.0.0.1:8001/ in your browser (Ctrl+C here stops it)."
& .\.venv\Scripts\python.exe -m uvicorn whymoved.api:create_app --factory --app-dir src --port 8001
