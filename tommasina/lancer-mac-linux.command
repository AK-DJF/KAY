#!/bin/bash
cd "$(dirname "$0")"
python3 -m pip install -q -r requirements.txt
(sleep 2; (xdg-open http://127.0.0.1:8001 || open http://127.0.0.1:8001) >/dev/null 2>&1) &
python3 app.py
