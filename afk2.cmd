@echo off
rem One-click AFK2 takeover: pause parent, fork headless continuation, keep App alive.
rem Wait without a time limit for confirmed parent stop before starting the fork.
rem (ASCII only - cmd.exe misreads UTF-8 Chinese comments under GBK codepage)
cd /d %~dp0
python supervise.py --adopt last --quick --fork %*
if errorlevel 1 pause
