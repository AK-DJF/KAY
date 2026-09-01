@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo   Kikou - Numerisation - Installation
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERREUR] Python n'est pas installe ou pas dans le PATH.
    echo Installez Python depuis https://www.python.org/downloads/
    echo IMPORTANT : cochez "Add python.exe to PATH" pendant l'installation.
    echo.
    pause
    exit /b 1
)

echo Installation des dependances...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERREUR] L'installation des dependances a echoue. Voir le message ci-dessus.
    pause
    exit /b 1
)

echo.
echo Demarrage du serveur sur http://127.0.0.1:8003 ...
echo (Laissez cette fenetre ouverte tant que vous utilisez l'application)
echo.

start "" http://127.0.0.1:8003
python app.py

pause
