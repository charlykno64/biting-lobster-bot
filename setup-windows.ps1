$ErrorActionPreference = "Stop"

Write-Host "Creating virtual environment (.venv)..."
python -m venv .venv

Write-Host "Activating virtual environment..."
& .\.venv\Scripts\Activate.ps1

Write-Host "Upgrading pip..."
python -m pip install --upgrade pip

Write-Host "Installing Epic 1 dependencies..."
pip install flet playwright playwright-stealth python-dotenv

Write-Host "Installing Playwright Chromium browser..."
python -m playwright install chromium

Write-Host "Setup completed successfully."
