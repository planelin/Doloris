@echo off
rem Doloris (ドロリス) - Desktop Companion & Autonomous Supervisor Launcher
rem (ASCII only - cmd.exe misreads UTF-8 Chinese comments under GBK codepage)
cd /d %~dp0

if "%1"=="app" goto do_app
if "%1"=="goal" goto do_goal
if "%1"=="fork" goto do_fork
if "%1"=="resume" goto do_resume
if "%1"=="gui" goto do_gui

rem If first parameter is a digit 1-9 or does not start with -, treat as adopt target
if not "%1"=="" (
    set "FIRST=%1"
    setlocal enabledelayedexpansion
    if not "!FIRST:~0,1!"=="-" (
        shift
        python supervise.py --adopt !FIRST! --quick --fork %*
        goto end
    )
    endlocal
)

rem Default: Fork mode with session list/interactive adopt
python supervise.py --adopt last --quick --fork %*
goto end

:do_app
shift
start "" pythonw -m doloris_app.main %*
goto end

:do_goal
set "TARGET=last"
if not "%2"=="" (
    set "SECOND=%2"
    setlocal enabledelayedexpansion
    if not "!SECOND:~0,1!"=="-" (
        set "TARGET=%2"
        shift
    )
)
shift
python supervise.py --adopt %TARGET% --quick --goal %*
goto end

:do_fork
set "TARGET=last"
if not "%2"=="" (
    set "SECOND=%2"
    setlocal enabledelayedexpansion
    if not "!SECOND:~0,1!"=="-" (
        set "TARGET=%2"
        shift
    )
)
shift
python supervise.py --adopt %TARGET% --quick --fork %*
goto end

:do_resume
set "TARGET=last"
if not "%2"=="" (
    set "SECOND=%2"
    setlocal enabledelayedexpansion
    if not "!SECOND:~0,1!"=="-" (
        set "TARGET=%2"
        shift
    )
)
shift
python supervise.py --adopt %TARGET% --quick %*
goto end

:do_gui
set "TARGET=last"
if not "%2"=="" (
    set "SECOND=%2"
    setlocal enabledelayedexpansion
    if not "!SECOND:~0,1!"=="-" (
        set "TARGET=%2"
        shift
    )
)
shift
python supervise.py --adopt %TARGET% --quick --gui %*
goto end

:end
if errorlevel 1 pause
