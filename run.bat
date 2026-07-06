@echo off
title Keyword News Scraper Launcher
echo ===================================================
echo      KEYWORD NEWS SCRAPER LAUNCHER
echo ===================================================
echo.

:: Check if Virtual Environment already exists
if exist .venv\Scripts\python.exe (
    echo [INFO] Found existing virtual environment.
    set "PYTHON_EXE=.venv\Scripts\python.exe"
    goto env_ready
)

:: If .venv doesn't exist, search for a system Python command to create it
set "PYTHON_CMD="

python --version >nul 2>&1
if %errorlevel% equ 0 (
    set "PYTHON_CMD=python"
    goto create_env
)

py --version >nul 2>&1
if %errorlevel% equ 0 (
    set "PYTHON_CMD=py"
    goto create_env
)

:: No Python found
echo [ERROR] Python is not installed or not in your PATH.
echo Please install Python 3.9+ from python.org and try again.
pause
exit /b 1

:create_env
echo [INFO] Creating Python virtual environment using %PYTHON_CMD%...
%PYTHON_CMD% -m venv .venv
if %errorlevel% neq 0 (
    echo [ERROR] Failed to create virtual environment.
    pause
    exit /b 1
)
set "PYTHON_EXE=.venv\Scripts\python.exe"

:env_ready
:: Check if .env file exists; if not, create it from template
if not exist .env (
    if exist .env.example (
        echo [INFO] Creating .env file from .env.example template...
        copy .env.example .env >nul
        echo [SUCCESS] Default .env created. Please configure your PostgreSQL connection in .env.
    ) else (
        echo [WARNING] .env and .env.example not found. Creating a default .env file...
        echo DATABASE_URL=postgresql://postgres:postgres@localhost:5432/keyword_scraper > .env
        echo API_TOKEN=changeme >> .env
        echo [SUCCESS] Default .env created.
    )
)

echo [INFO] Checking / Installing dependencies...
"%PYTHON_EXE%" -m pip install --upgrade pip
"%PYTHON_EXE%" -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [WARNING] Dependency installation failed or warnings occurred.
)

:: ── Start Tor Daemon if not already running ───────────────────
netstat -ano | findstr LISTENING | findstr :9050 >nul 2>&1
if %errorlevel% equ 0 (
    echo [INFO] Tor is already running on port 9050.
    goto tor_start_done
)

echo [INFO] Tor is not running. Attempting to start Tor...
set "TOR_EXE="
where tor >nul 2>&1
if %errorlevel% equ 0 (
    set "TOR_EXE=tor"
)
if not defined TOR_EXE if exist "%~dp0backend\tor\tor\tor.exe" set "TOR_EXE=%~dp0backend\tor\tor\tor.exe"
if not defined TOR_EXE if exist "%USERPROFILE%\Desktop\Tor Browser\Browser\TorBrowser\Tor\tor.exe" set "TOR_EXE=%USERPROFILE%\Desktop\Tor Browser\Browser\TorBrowser\Tor\tor.exe"
if not defined TOR_EXE if exist "%USERPROFILE%\OneDrive\Desktop\Tor Browser\Browser\TorBrowser\Tor\tor.exe" set "TOR_EXE=%USERPROFILE%\OneDrive\Desktop\Tor Browser\Browser\TorBrowser\Tor\tor.exe"
if not defined TOR_EXE if exist "%LOCALAPPDATA%\Tor Browser\Browser\TorBrowser\Tor\tor.exe" set "TOR_EXE=%LOCALAPPDATA%\Tor Browser\Browser\TorBrowser\Tor\tor.exe"
if not defined TOR_EXE if exist "%APPDATA%\Tor Browser\Browser\TorBrowser\Tor\tor.exe" set "TOR_EXE=%APPDATA%\Tor Browser\Browser\TorBrowser\Tor\tor.exe"
if not defined TOR_EXE if exist "C:\Program Files\Tor Browser\Browser\TorBrowser\Tor\tor.exe" set "TOR_EXE=C:\Program Files\Tor Browser\Browser\TorBrowser\Tor\tor.exe"
if not defined TOR_EXE if exist "C:\Program Files (x86)\Tor Browser\Browser\TorBrowser\Tor\tor.exe" set "TOR_EXE=C:\Program Files (x86)\Tor Browser\Browser\TorBrowser\Tor\tor.exe"
if not defined TOR_EXE if exist "D:\Tor Browser\Browser\TorBrowser\Tor\tor.exe" set "TOR_EXE=D:\Tor Browser\Browser\TorBrowser\Tor\tor.exe"
if not defined TOR_EXE if exist "E:\Tor Browser\Browser\TorBrowser\Tor\tor.exe" set "TOR_EXE=E:\Tor Browser\Browser\TorBrowser\Tor\tor.exe"

:: Recursive search in Downloads and Desktop
if not defined TOR_EXE (
    for /R "%USERPROFILE%\Downloads" %%i in (tor.exe) do if exist "%%i" (
        set "TOR_EXE=%%i"
        goto tor_found
    )
)
if not defined TOR_EXE (
    for /R "%USERPROFILE%\Desktop" %%i in (tor.exe) do if exist "%%i" (
        set "TOR_EXE=%%i"
        goto tor_found
    )
)

:tor_found
if not defined TOR_EXE (
    echo [INFO] Tor not found. Automatically setting up Tor SOCKS5 proxy...
    "%PYTHON_EXE%" backend\tor_setup.py
    if exist "%~dp0backend\tor\tor\tor.exe" set "TOR_EXE=%~dp0backend\tor\tor\tor.exe"
)

if defined TOR_EXE (
    echo [INFO] Found Tor executable: "%TOR_EXE%"
    echo [INFO] Starting Tor background process on port 9050...
    if "%TOR_EXE%"=="tor" (
        start "Tor Daemon" /min tor
    ) else (
        for %%I in ("%TOR_EXE%") do start "Tor Daemon" /d "%%~dpI" /min "%%~nxI"
    )
    :: Wait a few seconds for Tor daemon initialization
    ping 127.0.0.1 -n 6 >nul
) else (
    echo [WARNING] Tor executable not found.
    echo [WARNING] Please start Tor Browser or the Tor system service manually if you want Tor routing.
)

:tor_start_done

echo.
echo [INFO] Starting Keyword News Scraper Backend Server...
echo [INFO] The server will run on http://127.0.0.1:8000
echo [INFO] Press Ctrl+C in this terminal window to stop the server.
echo.

:: Wait 2 seconds and launch browser in parallel
start "" http://127.0.0.1:8000

:: Start Uvicorn Server
"%PYTHON_EXE%" -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload

pause
