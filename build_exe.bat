@echo off
rem Builds "dist\Vibe Launcher\Vibe Launcher.exe" (a standalone app, no Python needed to run it).
title Building Vibe Launcher
cd /d "%~dp0"
set PY=py
where py >nul 2>nul || set PY=python
rem Output folder; set VIBE_DIST to build somewhere else (e.g. a shorter path).
if not defined VIBE_DIST set "VIBE_DIST=%~dp0dist"

echo Installing build tools (one time)...
%PY% -m pip install --user --quiet --no-warn-script-location "minecraft-launcher-lib>=7" "pywebview>=5" pyinstaller pillow || goto :fail

echo Drawing the icon...
%PY% tools\make_icon.py || goto :fail

echo Building the app (takes a minute)...
%PY% -m PyInstaller --noconfirm --clean --windowed --onedir ^
  --name "Vibe Launcher" ^
  --icon "%~dp0assets\vibe.ico" ^
  --add-data "%~dp0ui.html;." ^
  --add-data "%~dp0assets;assets" ^
  --paths "%~dp0." ^
  --collect-submodules vibe_core ^
  --hidden-import webview ^
  --collect-submodules webview ^
  --collect-data webview ^
  --collect-binaries webview ^
  --hidden-import clr ^
  --collect-all pythonnet ^
  --collect-all clr_loader ^
  --collect-submodules minecraft_launcher_lib ^
  --collect-data minecraft_launcher_lib ^
  --workpath "%TEMP%\vibe-launcher-build" ^
  --specpath "%TEMP%\vibe-launcher-build" ^
  --distpath "%VIBE_DIST%" ^
  "%~dp0launcher.py" || goto :fail

echo.
echo Done: %VIBE_DIST%\Vibe Launcher\Vibe Launcher.exe
echo Keep the whole "Vibe Launcher" folder together; you can pin the .exe to Start or the taskbar.
pause
exit /b 0

:fail
echo.
echo Build failed - see the messages above.
pause
exit /b 1
