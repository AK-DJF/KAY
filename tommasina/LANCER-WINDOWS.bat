@echo off
cd /d "%~dp0"
if not exist app.py (
  echo ERREUR : app.py introuvable. Extrayez d'abord le ZIP ^(clic droit ^> Extraire tout^), puis relancez.
  pause
  exit /b 1
)
python -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo ERREUR pendant l'installation des dependances. Verifiez que Python est installe ^(case "Add to PATH"^).
  pause
  exit /b 1
)
start "" cmd /c "timeout /t 4 /nobreak >nul & start http://127.0.0.1:8001"
python app.py
pause
