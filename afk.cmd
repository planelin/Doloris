@echo off
rem One-click AFK takeover: adopt latest codex session, quick mode.
rem (ASCII only - cmd.exe misreads UTF-8 Chinese comments under GBK codepage)
cd /d %~dp0
python supervise.py --adopt last --quick %*
