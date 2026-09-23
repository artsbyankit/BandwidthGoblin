@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PY="

REM 1) py launcher wins when present
where py >nul 2>nul
if %errorlevel%==0 set "PY=py"

REM 1b) per-user py launcher (installed without admin)
if not defined PY (
  if exist "%LocalAppData%\Programs\Python\Launcher\py.exe" set "PY=%LocalAppData%\Programs\Python\Launcher\py.exe"
)

REM 2) python on PATH - but reject the Microsoft Store stub (it lies in `where`
REM    but exits nonzero because the real interpreter is missing)
if not defined PY (
  where python >nul 2>nul
  if %errorlevel%==0 (
    python -c "import sys" >nul 2>nul
    if !errorlevel!==0 set "PY=python"
  )
)

REM 3) user-scope installs (winget / python.org per-user). `dir` cannot glob
REM    middle path segments, so enumerate known versions explicitly.
if not defined PY (
  for %%v in (313 312 311 310 39 38) do (
    if not defined PY (
      if exist "%LocalAppData%\Programs\Python\Python%%v\python.exe" (
        "%LocalAppData%\Programs\Python\Python%%v\python.exe" -c "import sys" >nul 2>nul
        if !errorlevel!==0 set "PY=%LocalAppData%\Programs\Python\Python%%v\python.exe"
      )
    )
  )
)

REM 4) machine-wide installs
if not defined PY (
  for %%v in (313 312 311 310 39 38) do (
    if not defined PY (
      if exist "%ProgramFiles%\Python%%v\python.exe" (
        "%ProgramFiles%\Python%%v\python.exe" -c "import sys" >nul 2>nul
        if !errorlevel!==0 set "PY=%ProgramFiles%\Python%%v\python.exe"
      )
    )
  )
)

REM 5) leftover: plain python3 that did not survive the PATH sanity check
if not defined PY (
  where python3 >nul 2>nul
  if %errorlevel%==0 (
    python3 -c "import sys" >nul 2>nul
    if !errorlevel!==0 set "PY=python3"
  )
)

if not defined PY (
  echo.
  echo   Python was not found on this machine.
  echo.
  echo   Note: the Microsoft Store "python" shortcut does NOT count - disable the
  echo   App execution alias in Settings ^> Apps ^> Advanced app settings first.
  where winget >nul 2>nul
  if !errorlevel!==0 (
    choice /C YN /M "Install Python 3.13 now via winget"
    if errorlevel 2 exit /b 1
    winget install --id Python.Python.3.13 --exact --silent --accept-package-agreements --accept-source-agreements --scope user --disable-interactivity
    if !errorlevel!==0 (
      echo.
      echo   Python installed - starting BandwidthGoblin...
      ping 127.0.0.1 -n 2 >nul
      call "%~f0"
      exit /b
    )
    echo.
    echo   winget install failed - install manually from https://www.python.org/downloads/
  ) else (
    echo   Install it with one of these, then re-run RUN.bat:
    echo     winget install Python.Python.3.13
    echo     https://www.python.org/downloads/
  )
  pause
  exit /b 1
)

where wt.exe >nul 2>nul
if %errorlevel%==0 (
  wt.exe -d "%CD%" cmd /c ""%PY%" bandwidthgoblin.py || pause"
) else (
  "%PY%" bandwidthgoblin.py
  pause
)