@echo off
setlocal
set "PYTHONDONTWRITEBYTECODE=1"
py -3 "%~dp0verify_hook.py" 2>nul
if errorlevel 1 python "%~dp0verify_hook.py" 2>nul
if errorlevel 1 echo {}
