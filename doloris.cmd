@echo off
rem Doloris (ドロリス) - Desktop Companion & Autonomous Supervisor Launcher
rem (ASCII only - cmd.exe misreads UTF-8 Chinese comments under GBK codepage)
cd /d %~dp0

if "%1"=="fork" goto do_fork
if "%1"=="resume" goto do_resume
if "%1"=="gui" goto do_gui

rem Default: Fork mode (recommended - keep App alive, pause parent, fork headless)
python supervise.py --adopt last --quick --fork %*
goto end

:do_fork
shift
python supervise.py --adopt last --quick --fork %*
goto end

:do_resume
shift
python supervise.py --adopt last --quick %*
goto end

:do_gui
shift
python supervise.py --adopt last --quick --gui %*
goto end

:end
if errorlevel 1 pause
