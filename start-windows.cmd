@echo off
setlocal
cd /d "%~dp0"
if defined MONITOR_PYTHON (
    "%MONITOR_PYTHON%" launch_windows.py %*
    goto done
)
where py >nul 2>&1
if not errorlevel 1 (
    py -3 launch_windows.py %*
    goto done
)
where python >nul 2>&1
if not errorlevel 1 (
    python launch_windows.py %*
    goto done
)
echo Python 3.10+ was not found. Install Python or set MONITOR_PYTHON to python.exe.
:done
if errorlevel 1 pause
endlocal
