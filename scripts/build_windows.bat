@echo off
REM PyHakoCLI Windows Build and Test Script
REM Run this from Windows (double-click or cmd.exe)
REM Works from any location - auto-detects project path

REM Get the directory where this script is located
set SCRIPT_DIR=%~dp0
set PROJECT_DIR=%SCRIPT_DIR%..
set BUILD_DIR=%PROJECT_DIR%\dist

echo ============================================
echo  PyHakoCLI Windows Build ^& Test
echo ============================================
echo.

REM Check if uv is installed
where uv >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo ERROR: uv not found. Install from: https://docs.astral.sh/uv/
    pause
    exit /b 1
)

echo [1/3] Navigating to project...
pushd "%PROJECT_DIR%"
if %ERRORLEVEL% neq 0 (
    echo ERROR: Cannot access project path: %PROJECT_DIR%
    pause
    exit /b 1
)

echo      Project: %CD%
echo.

echo [2/3] Preparing workspace...
set WORKSPACE_ROOT=%TEMP%\PyHako_Workspace
set WORKSPACE_CLI=%WORKSPACE_ROOT%\PyHakoCLI
set WORKSPACE_LIB=%WORKSPACE_ROOT%\PyHako

if exist "%WORKSPACE_ROOT%" rmdir /s /q "%WORKSPACE_ROOT%"
mkdir "%WORKSPACE_ROOT%"

@REM Robocopy flags:
@REM /E - recursive, /XD - exclude dirs, /R:1 /W:1 - retry once wait 1s
@REM Exclude: .venv, dist, build, .git, auth_data, output, __pycache__, .pytest_cache

echo      Copying PyHakoCLI to workspace...
robocopy "%PROJECT_DIR%" "%WORKSPACE_CLI%" /E /XD .venv dist build .git auth_data output __pycache__ .pytest_cache .idea .vscode /R:1 /W:1 /NFL /NDL /NJH /NJS
if %ERRORLEVEL% geq 8 (
    echo ERROR: Robocopy failed for PyHakoCLI
    pause
    exit /b 1
)

echo      Copying PyHako (dependency) to workspace...
robocopy "%PROJECT_DIR%\..\PyHako" "%WORKSPACE_LIB%" /E /XD .venv dist build .git auth_data output __pycache__ .pytest_cache .idea .vscode /R:1 /W:1 /NFL /NDL /NJH /NJS
if %ERRORLEVEL% geq 8 (
    echo ERROR: Robocopy failed for PyHako
    pause
    exit /b 1
)

echo.
echo [3/4] Building Windows executable...
pushd "%WORKSPACE_CLI%"
uv run python scripts/build_local.py
if %ERRORLEVEL% neq 0 (
    echo ERROR: Build failed!
    popd
    pause
    exit /b 1
)





echo [4/5] Running smoke tests (verify_build.py)...
REM Unset inherited environment variables from the parent 'uv run' that triggered this script.
REM This ensures the nested 'uv run' treats the temp workspace as an isolated project.
set VIRTUAL_ENV=
set UV_PROJECT_ENVIRONMENT=

uv run python scripts/verify_build.py --skip-build
if %ERRORLEVEL% neq 0 (
    echo ERROR: Smoke tests failed!
    popd
    pause
    exit /b 1
)
popd

echo.
echo [5/5] Copying artifacts back...
if not exist "%BUILD_DIR%" mkdir "%BUILD_DIR%"
copy /Y "%WORKSPACE_CLI%\dist\pyhako-cli-windows.exe" "%BUILD_DIR%\" >nul
echo      Artifact copied to %BUILD_DIR%

popd

echo.
echo ============================================
echo  Build complete!
echo  Executable: %BUILD_DIR%\pyhako-cli-windows.exe
echo ============================================
echo.
REM Only pause if manually double-clicked (detection hard in bat, so removing for automation)
REM or just keep typical "Press any key" if user wants to see output?
REM For automated test runners, pause is bad. But user is running manually.
REM I will remove pause to prevent hanging if invoked from scripts.
exit /b 0
