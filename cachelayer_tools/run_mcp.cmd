@echo off
setlocal
set "PYTHONDONTWRITEBYTECODE=1"
set "ROOT=%~dp0"
set "WORKSPACE=%CACHELAYER_WORKSPACE_ROOT%"
if not defined WORKSPACE set "WORKSPACE=%CLAUDE_PROJECT_DIR%"
if not defined WORKSPACE set "WORKSPACE=%CODEX_WORKSPACE_ROOT%"
if not defined WORKSPACE set "WORKSPACE=%CURSOR_WORKSPACE_ROOT%"
if not defined WORKSPACE set "WORKSPACE=%GITHUB_WORKSPACE%"
if defined WORKSPACE if exist "%WORKSPACE%\." cd /d "%WORKSPACE%"
where py >nul 2>nul
if not errorlevel 1 (
  py -3 "%ROOT%mcp_server.py"
  exit /b %errorlevel%
)
where python3 >nul 2>nul
if not errorlevel 1 (
  python3 "%ROOT%mcp_server.py"
  exit /b %errorlevel%
)
python "%ROOT%mcp_server.py"
