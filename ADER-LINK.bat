@echo off
title ADER LINK
cd /d "%~dp0"
echo Ouvrez http://127.0.0.1:8080 dans le navigateur (ne fermez pas cette fenetre)
:start
python -m app serve --host 0.0.0.0
if %errorlevel%==3 (
  echo Mise a jour installee, redemarrage...
  goto start
)
pause
