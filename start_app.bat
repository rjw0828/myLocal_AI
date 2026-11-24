@echo off
setlocal

rem Run from this batch file's directory, regardless of current directory.
cd /d "%~dp0"

set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
rem Explicit opt-in only. Reuse an existing Agent-capable Python if configured.
if "%~1"=="--agent" set "LOCAL_AI_AGENT_ENABLED=1"
if "%~1"=="--no-agent" set "LOCAL_AI_AGENT_ENABLED=0"
if not "%~1"=="" if not "%~1"=="--agent" if not "%~1"=="--no-agent" (
    echo [ERROR] Usage: start_app.bat [--agent ^| --no-agent]
    exit /b 2
)
if "%LOCAL_AI_AGENT_ENABLED%"=="1" if defined LOCAL_AI_AGENT_PYTHON set "PYTHON_EXE=%LOCAL_AI_AGENT_PYTHON%"
if not exist "%PYTHON_EXE%" (
    echo [ERROR] Local Python environment not found:
    echo         "%PYTHON_EXE%"
    echo Create .venv in the project folder first.
    pause
    exit /b 1
)

if not exist "%~dp0app.py" (
    echo [ERROR] app.py was not found next to start_app.bat.
    pause
    exit /b 1
)

rem Preserve an explicit approval path; otherwise use the owner's local file.
if not defined LOCAL_AI_MCP_APPROVALS (
    if exist "%~dp0data\mcp_approved_profiles.json" set "LOCAL_AI_MCP_APPROVALS=%~dp0data\mcp_approved_profiles.json"
)

echo [Local AI Lab] Starting Flask Web Console...
if "%LOCAL_AI_AGENT_ENABLED%"=="1" (echo [Local AI Lab] Agent opt-in requested. No MCP server starts automatically.) else (echo [Local AI Lab] Agent disabled. Use --agent to opt in.)
echo [Local AI Lab] http://127.0.0.1:5000
echo [Local AI Lab] Press Ctrl+C to stop the server.
echo.
"%PYTHON_EXE%" "%~dp0app.py"
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo [Local AI Lab] Server stopped. Exit code: %EXIT_CODE%
pause
exit /b %EXIT_CODE%
