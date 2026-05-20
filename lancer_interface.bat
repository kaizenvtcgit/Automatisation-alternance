@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist ".env" (
  if exist ".env.example" (
    copy /Y ".env.example" ".env" >nul
  )
)

if not exist ".venv\Scripts\pythonw.exe" (
  echo Installation locale requise avant le premier lancement.
  echo.
  start "" "%~dp0installer_partage_local.bat"
  exit /b 0
)

wscript.exe "%~dp0lancer_interface.vbs"
