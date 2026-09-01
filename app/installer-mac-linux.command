#!/bin/bash
cd "$(dirname "$0")"

echo "============================================"
echo "  Kikou - Numerisation - Installation"
echo "============================================"
echo

PYTHON=""
if command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
elif command -v python >/dev/null 2>&1; then
    PYTHON=python
else
    echo "[ERREUR] Python n'est pas installe."
    echo "Installez Python depuis https://www.python.org/downloads/"
    read -p "Appuyez sur Entree pour fermer..."
    exit 1
fi

echo "Installation des dependances..."
"$PYTHON" -m pip install --upgrade pip >/dev/null
"$PYTHON" -m pip install -r requirements.txt
if [ $? -ne 0 ]; then
    echo
    echo "[ERREUR] L'installation des dependances a echoue. Voir le message ci-dessus."
    read -p "Appuyez sur Entree pour fermer..."
    exit 1
fi

echo
echo "Demarrage du serveur sur http://127.0.0.1:8003 ..."
echo "(Laissez cette fenetre ouverte tant que vous utilisez l'application)"
echo

( sleep 2
  if command -v open >/dev/null 2>&1; then
      open http://127.0.0.1:8003
  elif command -v xdg-open >/dev/null 2>&1; then
      xdg-open http://127.0.0.1:8003
  fi
) &

"$PYTHON" app.py
