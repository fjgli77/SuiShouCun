@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Building Windows desktop EXE (this takes a few minutes)...
python build_exe.py
if errorlevel 1 pause
echo.
echo Output: dist\随手存-电脑端.exe
pause
