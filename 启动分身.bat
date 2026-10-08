@echo off
setlocal
cd /d "%~dp0"
title FenShen AI Twin

echo ==================================================
echo   Private AI Twin - Launcher
echo   Web UI : http://127.0.0.1:8899
echo   Close this window to stop the server.
echo ==================================================
echo.

rem -- locate python --
set "PYCMD="
where py >nul 2>nul && set "PYCMD=py -3"
if not defined PYCMD where python >nul 2>nul && set "PYCMD=python"
if not defined PYCMD where python3 >nul 2>nul && set "PYCMD=python3"

if not defined PYCMD (
    echo [ERROR] Python was not found on this computer.
    echo Install Python 3.8 or newer from https://www.python.org/downloads/
    echo and make sure "Add Python to PATH" is checked during install,
    echo then run this launcher again.
    echo.
    pause
    exit /b 1
)

echo Using : %PYCMD%
echo Press Ctrl+C in this window to stop the twin anytime.
echo.

if "%FENSHEN_NO_BROWSER%"=="1" (
    %PYCMD% backend.py --no-browser
) else (
    %PYCMD% backend.py
)
set "CODE=%ERRORLEVEL%"
echo.
if "%CODE%"=="0" (
    echo Server stopped.
) else (
    if "%CODE%"=="2" (
        echo [ERROR] Port 8899 is in use or cannot be bound.
        echo Close other FenShen windows, then retry.
    ) else (
        echo Backend exited with code %CODE%. Check messages above.
    )
)
echo.
pause
endlocal