@echo off
rem Kill takeover: select session, verify boundary, auto-close App, headless resume.
rem (ASCII only - cmd.exe misreads UTF-8 Chinese comments under GBK codepage)
cd /d %~dp0
python supervise.py --adopt last --quick %*
if errorlevel 1 pause
