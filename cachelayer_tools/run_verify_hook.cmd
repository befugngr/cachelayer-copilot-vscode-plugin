@echo off
setlocal
if /I not "%CACHELAYER_VERIFY%"=="1" (
  echo {}
  exit /b 0
)
set "PYTHONDONTWRITEBYTECODE=1"
py -3 "%~dp0verify_hook.py" 2>nul
if errorlevel 1 python "%~dp0verify_hook.py" 2>nul
if errorlevel 1 echo {}
