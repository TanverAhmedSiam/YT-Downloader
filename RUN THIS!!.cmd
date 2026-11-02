@echo off
title ytdl studio
cd /d "%~dp0"
echo Starting ytdl studio...
echo.
echo A browser window will open by itself. Keep THIS window open while you use it.
echo Close this window (or press Ctrl+C) to stop the studio.
echo.
".venv\Scripts\python.exe" -m ytdl gui
echo.
echo The studio has stopped. You can close this window.
pause
