# services/extractor_ia.py
# Extraction des relevés bancaires par IA de vision (OpenRouter) — même principe et même
# clé/configuration que services/facture_extractor.py pour les factures : un seul appel
# image -> JSON structuré par lot de pages, potentiellement plus fiable que les parseurs
# dédiés (parsers/*.py) sur une banque non calibrée ou un scan de mauvaise qualité, et
# sans avoir besoin d'écrire un parseur pour chaque nouvelle banque. Utilisée en premier
# si OPENROUTER_API_KEY est configurée ; en cas d'échec (réseau, clé invalide, réponse
# inexploitable...) ou si aucune clé n'est configurée, l'appelant (services/extractor.py)
# retombe silencieusement sur les parseurs locaux existants.

import base64
import io
import json
import os
from datetime import date
from pathlib import Path
from typing import Optional

import pdfplumber
from PIL import Image

from parsers.base import Transaction

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "mistralai/mistral-small-2603").strip()
OPENROUTER_TIMEOUT = float(os.environ.get("OPENROUTER_TIMEOUT", "90"))

# Nombre de pages envoyées par appel — les relevés font rarement plus de 5 pages, mais on
# découpe par lots pour rester dans une taille de requête raisonnable sur les relevés plus
# longs (et continuer à progresser même si un lot échoue).
PAGES_PAR_APPEL = 3
PAGES_MAX = 20  # au-delà, l'IA n'est plus tentée — retombe directement sur les parseurs locaux


class ExtractionIAError(Exception):
    pass


PROMPT_IA = """Tu es un assistant d'extraction de données comptables. Analyse cette/ces \
image(s) de relevé de compte bancaire et réponds UNIQUEMENT avec un objet JSON strict, \
sans texte autour, ni bloc de code, avec exactement ces champs :

{
  "banque": "nom de la banque tel qu'imprimé sur le relevé, ou null",
  "transactions": [
    {"date": "YYYY-MM-DD", "libelle": "texte", "debit": nombre ou null, "credit": nombre ou null},
    ...
  ]
}

Règles :
- Une entrée par ligne de mouvement visible sur les images, dans l'ordre où elles apparaissent
  (ignore les lignes de solde/total/en-tête).
- "date" est la date d'opération (pas la date de valeur si les deux sont présentes) ; déduis
  l'année si elle n'est pas répétée sur chaque ligne (reprends-la de l'en-tête/période du
  relevé, ou d'une date complète présente ailleurs sur le document).
- Un montant va dans "debit" OU "credit" selon la colonne où il apparaît sur le relevé,
  jamais les deux à la fois pour une même ligne.
- Les montants sont des nombres (point décimal, sans symbole monétaire ni séparateur de
  milliers).
- N'invente jamais une ligne ou une valeur : si un champ est illisible, mets null."""


def _normaliser_nombre(valeur) -> Optional[float]:
    if valeur in (None, "", "null"):
        return None
    try:
        return float(valeur)
    except (TypeError, ValueError):
        return None


def _pages_en_images_png(chemin: Path, resolution: int = 150) -> list[bytes]:
    images = []
    with pdfplumber.open(chemin) as pdf:
        for page in pdf.pages:
            img = page.to_image(resolution=resolution).original.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            images.append(buf.getvalue())
    return images


def _extraire_json_reponse(texte: str) -> dict:
    debut, fin = texte.find("{"), texte.rfind("}")
    if debut == -1 or fin == -1 or fin < debut:
        raise ExtractionIAError("Réponse IA sans JSON exploitable")
    return json.loads(texte[debut:fin + 1])


def _appeler_modele_vision(images_png: list[bytes]) -> dict:
    import httpx

    contenu = [{"type": "text", "text": PROMPT_IA}]
    for png in images_png:
        data_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        contenu.append({"type": "image_url", "image_url": {"url": data_url}})

    try:
        reponse = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "X-Title": "Kwika - Numerisation releves",
            },
            json={
                "model": OPENROUTER_MODEL,
                "temperature": 0,
                "messages": [{"role": "user", "content": contenu}],
            },
            timeout=OPENROUTER_TIMEOUT,
        )
        reponse.raise_for_status()
        texte = reponse.json()["choices"][0]["message"]["content"]
        return _extraire_json_reponse(texte)
    except ExtractionIAError:
        raise
    except Exception as e:
        raise ExtractionIAError(f"Appel OpenRouter échoué : {e}")


def extraire_transactions_ia(chemin_pdf: str) -> tuple[list[Transaction], str]:
    """Extraction par IA de vision. Lève ExtractionIAError sur tout échec (pas de clé,
    réseau, réponse inexploitable, ou aucune transaction reconnue) — c'est l'appelant qui
    décide de retomber sur les parseurs locaux dans ce cas."""
    if not OPENROUTER_API_KEY:
        raise ExtractionIAError("OPENROUTER_API_KEY non configurée")

    chemin = Path(chemin_pdf)
    nom_fichier = chemin.name
    images = _pages_en_images_png(chemin)
    if not images:
        raise ExtractionIAError("PDF vide (aucune page)")
    if len(images) > PAGES_MAX:
        raise ExtractionIAError(f"Relevé trop long pour l'extraction IA ({len(images)} pages)")

    transactions: list[Transaction] = []
    nom_banque: Optional[str] = None

    for i in range(0, len(images), PAGES_PAR_APPEL):
        lot = images[i:i + PAGES_PAR_APPEL]
        brut = _appeler_modele_vision(lot)

        if nom_banque is None:
            b = brut.get("banque")
            if b not in (None, "", "null"):
                nom_banque = str(b).strip()

        for t in brut.get("transactions") or []:
            date_txt = t.get("date")
            try:
                d = date.fromisoformat(str(date_txt)[:10])
            except (TypeError, ValueError):
                continue  # ligne sans date exploitable — ignorée plutôt que de deviner
            debit = _normaliser_nombre(t.get("debit"))
            credit = _normaliser_nombre(t.get("credit"))
            if debit is None and credit is None:
                continue
            transactions.append(Transaction(
                date=d, libelle=str(t.get("libelle") or "").strip(),
                debit=debit, credit=credit, solde=None,
                banque=nom_banque or "Banque (IA)", fichier_source=nom_fichier,
            ))

    if not transactions:
        raise ExtractionIAError("Aucune transaction reconnue par l'IA")

    banque_finale = nom_banque or "Banque (IA)"
    for tr in transactions:
        tr.banque = banque_finale
    return transactions, banque_finale
