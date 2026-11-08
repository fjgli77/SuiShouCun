@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Building Android APK with local Android SDK (no Gradle needed)...
python "%~dp0build_apk.py"
if errorlevel 1 pause
echo.
echo Output: dist\随手存-手机版.apk
pause
