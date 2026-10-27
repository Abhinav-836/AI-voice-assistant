$ErrorActionPreference = "Stop"

Write-Host "Setting up AI Voice Assistant on Windows..." -ForegroundColor Cyan

if (-not (Test-Path "venv")) {
    Write-Host "Creating Python virtual environment in .\venv..."
    python -m venv venv
}

Write-Host "Activating virtual environment and installing dependencies..."
& ".\venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\venv\Scripts\python.exe" -m pip install -r requirements.txt

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example — please fill in your API keys before running." -ForegroundColor Yellow
}

Write-Host "`nSetup complete!" -ForegroundColor Green
Write-Host "`nTo run the agent in console mode:" -ForegroundColor Cyan
Write-Host "  .\venv\Scripts\python.exe -m agent.main console"
Write-Host "`nTo run the agent with LiveKit WebRTC:" -ForegroundColor Cyan
Write-Host "  .\venv\Scripts\python.exe -m agent.main dev"
Write-Host "`nTo run the web dashboard server:" -ForegroundColor Cyan
Write-Host "  .\venv\Scripts\python.exe frontend/serve.py"
Write-Host "`nTo run the test suite:" -ForegroundColor Cyan
Write-Host "  .\venv\Scripts\python.exe -m pytest -v"
