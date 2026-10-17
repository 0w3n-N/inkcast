@echo off
rem Inkcast command for Windows: runs bin\inkcast with the Python launcher (py), or python.
setlocal
set "SCRIPT=%~dp0inkcast"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -B "%SCRIPT%" %*
) else (
  python -B "%SCRIPT%" %*
)
exit /b %errorlevel%
