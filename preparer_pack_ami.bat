@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Alternance Auto - Preparation du pack ami

set "TARGET_ROOT=%~dp0partage_local"
set "TARGET_DIR=%TARGET_ROOT%\alternance-auto"

echo ==========================================
echo   Alternance Auto - Preparation du pack
echo ==========================================
echo.
echo Ce script cree un dossier propre a partager,
echo sans .env, sans historique local, sans exports runtime
echo et sans environnement virtuel.
echo.

if exist "%TARGET_DIR%" (
  echo [INFO] Un ancien pack existe deja.
  choice /C ON /M "Le remplacer ?"
  if errorlevel 2 exit /b 0
  rmdir /S /Q "%TARGET_DIR%"
)

if not exist "%TARGET_ROOT%" mkdir "%TARGET_ROOT%" >nul 2>nul

echo [INFO] Copie des fichiers du projet...
robocopy "%~dp0" "%TARGET_DIR%" /E /R:1 /W:1 ^
  /XD ".git" ".venv" "__pycache__" "logs" "browser_profile" "export" ".claude" "partage_local" ^
  /XF ".env" "historique_postulations.json" "app_server.pid" "server_stdout.log" "server_stderr.log" "*.pyc" "*.pyo" "*.pyd" >nul

if errorlevel 8 (
  echo [ERREUR] La copie du projet a echoue.
  echo.
  pause
  exit /b 1
)

echo [INFO] Recreation des dossiers de travail propres...
if not exist "%TARGET_DIR%\export" mkdir "%TARGET_DIR%\export" >nul 2>nul
if not exist "%TARGET_DIR%\export\messages" mkdir "%TARGET_DIR%\export\messages" >nul 2>nul
if not exist "%TARGET_DIR%\export\uploads" mkdir "%TARGET_DIR%\export\uploads" >nul 2>nul

copy /Y "%~dp0export\profil_recherche.example.json" "%TARGET_DIR%\export\profil_recherche.example.json" >nul
type nul > "%TARGET_DIR%\export\.gitkeep"
type nul > "%TARGET_DIR%\export\messages\.gitkeep"

echo [OK] Pack pret dans :
echo %TARGET_DIR%
echo.
echo Etapes conseillees :
echo 1. zippe le dossier "%TARGET_DIR%"
echo 2. envoie le zip a ton ami
echo 3. il lance installer_partage_local.bat
echo 4. il complete les Parametres
echo.
pause
exit /b 0
