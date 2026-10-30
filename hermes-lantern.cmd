@echo off
setlocal
set "PYTHONUTF8=1"
py -3 -u "%~dp0hermes_lantern.py" %*
exit /b %errorlevel%
