@echo off
cd /d "%~dp0"
if "%~1"=="" (
    echo.
    echo Faites glisser le fichier PDF du releve sur ce fichier DIAGNOSTIC.bat
    echo ^(pas besoin de taper quoi que ce soit^).
    echo.
    pause
    exit /b
)
python diagnostic_cih.py "%~1"
echo.
echo ============================================================
echo Copiez TOUT le texte affiche ci-dessus et collez-le dans le chat.
echo ============================================================
pause
