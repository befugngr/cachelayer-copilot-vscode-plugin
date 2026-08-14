@echo off
py -3 "%~dp0critic_hook.py" 2>nul
if errorlevel 1 python "%~dp0critic_hook.py" 2>nul
if errorlevel 1 echo {}
