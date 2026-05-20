@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Alternance Auto - Installation locale

echo ==========================================
echo   Alternance Auto - Installation locale
echo ==========================================
echo.

if not exist ".env" (
  if exist ".env.example" (
    copy /Y ".env.example" ".env" >nul
    echo [OK] Fichier .env cree a partir de .env.example
  ) else (
    echo [ERREUR] .env.example est introuvable.
    echo.
    pause
    exit /b 1
  )
) else (
  echo [OK] Fichier .env deja present
)

set "PYTHON_CMD="
where py >nul 2>nul
if not errorlevel 1 (
  py -3.12 --version >nul 2>nul
  if not errorlevel 1 (
    set "PYTHON_CMD=py -3.12"
  ) else (
    py --version >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=py"
  )
)

if not defined PYTHON_CMD (
  where python >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
  echo [ERREUR] Python est introuvable.
  echo Installe Python 3.12+ puis relance ce fichier.
  echo.
  pause
  exit /b 1
)

echo [OK] Python detecte via %PYTHON_CMD%

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo [INFO] Creation de l'environnement virtuel...
  call %PYTHON_CMD% -m venv .venv
  if errorlevel 1 (
    echo [ERREUR] Impossible de creer l'environnement virtuel.
    echo.
    pause
    exit /b 1
  )
) else (
  echo [OK] Environnement virtuel deja present
)

echo.
echo [INFO] Mise a jour de pip...
call ".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 (
  echo [ERREUR] Echec de la mise a jour de pip.
  echo.
  pause
  exit /b 1
)

echo.
echo [INFO] Installation des dependances Python...
call ".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo [ERREUR] Echec de l'installation des dependances.
  echo.
  pause
  exit /b 1
)

echo.
echo [INFO] Installation du navigateur Playwright (Chromium)...
call ".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 (
  echo [ERREUR] Echec de l'installation Playwright.
  echo.
  pause
  exit /b 1
)

if not exist "export" mkdir "export" >nul 2>nul
if not exist "export\messages" mkdir "export\messages" >nul 2>nul
if not exist "export\uploads" mkdir "export\uploads" >nul 2>nul

echo.
echo [OK] Installation terminee.
echo.
echo Prochaine etape :
echo - ouvre l'application
echo - complete l'onglet Parametres
echo - verifie le bloc "Configuration initiale" dans le dashboard
echo.
choice /C ON /M "Ouvrir l'application maintenant ?"
if errorlevel 2 goto :end
call "%~dp0lancer_interface.bat"

:end
echo.
pause
exit /b 0
