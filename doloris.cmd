@echo off
rem Doloris (ドロリス) - Desktop Companion & Autonomous Supervisor Launcher
rem (ASCII only - cmd.exe misreads UTF-8 Chinese comments under GBK codepage)
rem
rem This wrapper forwards ALL arguments verbatim. It must never consume a
rem subcommand word itself: cmd.exe "%*" ignores `shift`, so doing so used to
rem duplicate the subcommand into the child argv and break argparse.
rem Subcommand / adopt-target parsing lives in afk_supervisor.cli (Python side).
cd /d %~dp0
python supervise.py %*
if errorlevel 1 pause
