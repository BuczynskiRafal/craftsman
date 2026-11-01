@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "PLUGIN_ROOT=%%~fI"

set "PYTHON_CMD="
for %%C in (py python3 python) do (
  if "%%C"=="py" (
    py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
    if not errorlevel 1 (
      set "PYTHON_CMD=py -3"
      goto :found
    )
  ) else (
    where %%C >nul 2>&1
    if not errorlevel 1 (
      %%C -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
      if not errorlevel 1 (
        set "PYTHON_CMD=%%C"
        goto :found
      )
    )
  )
)

:found
if "%PYTHON_CMD%"=="" (
  echo Craftsman gate inactive: Python 3.10+ not found >&2
  exit /b 0
)

set "PYTHONPATH=%PLUGIN_ROOT%"
%PYTHON_CMD% -m craftsman.cli %*
