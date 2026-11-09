@echo off
title Portail Comptablio - lancement
cd /d "%~dp0"

rem Demarre tout Comptablio (portail + 5 applications). Meme contenu que le
rem lanceur du dossier parent, mais utilisable depuis ici.
rem "/D" fixe le dossier de depart sans repasser le chemin dans une chaine cmd :
rem le dossier utilisateur contient un "^" (C:\Users\rtyuiop^$) que cmd avalerait.

rem Identifiants KAY PV AGO / KAY JURIDIQUE et Fatoura Pro (a changer ici si besoin).
set "KAY_AUTH_USER=kawtar"
set "KAY_AUTH_PASS=NqIsaI7lEL5peQA1XAuD"
set "KAY_SECRET_KEY=e571ff77f4515de629e51fa5ab1058ed9fb36b15d3731c4ec044d78701513add"
set "FATOURA_AUTH_USER=kawtar"
set "FATOURA_AUTH_PASS=FatouraPro_2026"

echo Demarrage de Comptablio (portail + 5 applications)...

start "Portail Comptablio - serveur (ne pas fermer)" /D "%~dp0" cmd /k python app.py
start "KAY PV AGO - serveur (ne pas fermer)" /D "%~dp0..\KAY PV AGO" cmd /k python app.py
start "KAY JURIDIQUE - serveur (ne pas fermer)" /min /D "%~dp0..\KAY JURIDIQUE" cmd /k .venv\Scripts\python.exe app.py
start "Kikou Numerisation - serveur (ne pas fermer)" /min /D "%~dp0..\Application de numerisation - Maroc\app" cmd /k .venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8003
start "Fatoura Pro - serveur (ne pas fermer)" /min /D "%~dp0..\Fatoura Pro" cmd /k python app.py
start "E-COMPTA - serveur (ne pas fermer)" /min /D "%~dp0..\E-COMPTA" cmd /k python app.py

timeout /t 4 /nobreak >nul
start "" http://127.0.0.1:8090

echo.
echo Les 6 serveurs tournent chacun dans leur propre fenetre "...serveur (ne pas fermer)".
echo Pour tout arreter : fermez ces 6 fenetres.
echo Cette fenetre-ci peut etre fermee sans probleme.
pause
