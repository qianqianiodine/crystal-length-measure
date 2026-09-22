@echo off
rem ---------------------------------------------------------------------
rem  Keep this file ASCII-only, and saved with CRLF line endings.
rem
rem  Why: cmd.exe reads .bat files using the LOCAL codepage (CP936 on a
rem  Chinese Windows), but editors save UTF-8. The two disagree, so any
rem  Chinese written here shows up as garbage. `chcp 65001` does not fix
rem  it either -- cmd has its own bugs reading batch files under that
rem  codepage, and the script can even exit early.
rem
rem  So: all Chinese output lives in app/launch.py, printed by Python,
rem  which encodes to the console codepage correctly.
rem ---------------------------------------------------------------------

cd /d "%~dp0"

rem  This machine has no python on the system PATH -- call Anaconda directly.
set "PY=D:\anaconda3\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" -m app.launch

rem  Keep the window open so any error stays readable -- but only when there
rem  IS an error. launch.py exits 0 for the normal "already running, page
rem  opened" case, so clicking the shortcut stays a one-click action.
if errorlevel 1 pause
