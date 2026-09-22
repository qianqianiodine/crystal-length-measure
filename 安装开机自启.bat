@echo off
rem ---------------------------------------------------------------------
rem  Keep this file ASCII-only, and saved with CRLF line endings.
rem  See the launcher .bat in this folder for the full explanation: cmd.exe
rem  reads .bat files with the LOCAL codepage, so any Chinese written here
rem  turns into garbage. All Chinese output comes from Python instead
rem  (see app/autostart.py).
rem ---------------------------------------------------------------------

cd /d "%~dp0"

rem  This machine has no python on the system PATH -- call Anaconda directly.
set "PY=D:\anaconda3\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" -m app.autostart install

rem  Only stop for a keypress when something went wrong.
if errorlevel 1 pause
