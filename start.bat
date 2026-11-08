@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Starting SuiShouCun desktop (EXE)...
if exist "dist\随手存-电脑端.exe" (
  start "" "dist\随手存-电脑端.exe"
  echo Desktop app launched. PC and Android run independently on their own device.
) else (
  echo EXE not found, falling back to Python source mode...
  python -m media_dl --web --app %*
  if errorlevel 1 pause
)
