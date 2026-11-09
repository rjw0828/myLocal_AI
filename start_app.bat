@echo off
setlocal

rem Run from this batch file's directory, regardless of current directory.
cd /d "%~dp0"

set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
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
echo [Local AI Lab] http://127.0.0.1:5000
echo [Local AI Lab] Press Ctrl+C to stop the server.
echo.
"%PYTHON_EXE%" "%~dp0app.py"
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo [Local AI Lab] Server stopped. Exit code: %EXIT_CODE%
pause
exit /b %EXIT_CODE%
