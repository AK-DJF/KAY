@echo off
cd /d "%~dp0"
echo ============================================
echo   Kikou - Demarrage automatique avec Windows
echo ============================================
echo.
if not exist "DEMARRER-KIKOU.bat" (
    echo [ERREUR] DEMARRER-KIKOU.bat introuvable dans ce dossier.
    echo Placez ce fichier a cote de DEMARRER-KIKOU.bat et de app.py.
    pause
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$w=New-Object -ComObject WScript.Shell; foreach($d in @([Environment]::GetFolderPath('Startup'),[Environment]::GetFolderPath('Desktop'))){ $s=$w.CreateShortcut((Join-Path $d 'Kikou Numerisation.lnk')); $s.TargetPath=(Join-Path '%~dp0' 'DEMARRER-KIKOU.bat'); $s.WorkingDirectory='%~dp0'; $s.WindowStyle=7; $s.Save() }"
if errorlevel 1 (
    echo [ERREUR] Impossible de creer les raccourcis.
    pause
    exit /b 1
)
echo OK : Kikou demarrera automatiquement a chaque ouverture de session Windows.
echo Un raccourci "Kikou Numerisation" a aussi ete ajoute sur le Bureau.
echo.
echo Pour annuler : lancez DESACTIVER-DEMARRAGE-AUTO.bat
pause
