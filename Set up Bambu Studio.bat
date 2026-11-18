@echo off
title Set up Bambu Studio for the QIDI Q2 bridge
cd /d "%~dp0"
set PY=py -3
where py >NUL 2>&1
if errorlevel 1 set PY=python
if "%~1"=="elevated" goto admin
echo Installing the one extra package the Bambu Studio printer needs...
%PY% -m pip install --user --quiet --disable-pip-version-check cryptography
net session >NUL 2>&1
if not errorlevel 1 goto admin
echo.
echo Windows will now ask for permission so the bridge can be added to Bambu Studio. Click Yes.
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList 'elevated' -Verb RunAs"
goto end
:admin
%PY% -m qidi_bridge setup-studio
echo.
echo Now close this window and start the bridge again with "Start Bridge.bat".
pause
:end
