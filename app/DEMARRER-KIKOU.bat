@echo off
cd /d "%~dp0"
title Kikou - Numerisation
echo ============================================
echo   Kikou - Numerisation - Demarrage
echo ============================================
echo.
if not exist "app.py" (
    echo [ERREUR] app.py introuvable. Placez ce fichier dans le dossier qui contient app.py.
    pause
    exit /b 1
)
echo Demarrage du serveur sur http://127.0.0.1:8000 ...
echo Laissez cette fenetre ouverte tant que vous utilisez l application.
echo.
start "" cmd /c "timeout /t 5 /nobreak >nul & start http://127.0.0.1:8000"
py -3.12 app.py
echo.
echo L application s est arretee. Si un message d erreur est affiche ci-dessus, copiez-le.
pause
