@echo off
title QIDI Q2 Print Bridge
cd /d "%~dp0"
where py >NUL 2>&1
if not errorlevel 1 goto usepy
where python >NUL 2>&1
if not errorlevel 1 goto usepython
echo Python is not installed.
echo Get it free from https://www.python.org/downloads/ and tick "Add python.exe to PATH" during setup.
goto end
:usepy
py -3 -m qidi_bridge %*
goto end
:usepython
python -m qidi_bridge %*
:end
pause
