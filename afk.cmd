@echo off
rem 一键托管: 接管最近的codex会话, 快速挂机模式(零准备)
cd /d %~dp0
python supervise.py --adopt last --quick %*
