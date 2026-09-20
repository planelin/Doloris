@echo off
rem One-click AFK3 dual-headed supervision: keep Desktop App running, inject via GUI.
rem (ASCII only - cmd.exe misreads UTF-8 Chinese comments under GBK codepage)
cd /d %~dp0
python supervise.py --adopt last --quick --gui %*
if errorlevel 1 pause
