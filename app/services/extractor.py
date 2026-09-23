# services/extractor.py
# Orchestre l'extraction : reçoit un chemin PDF, retourne des Transaction[]
#
# Extension du 2026-08-27 (demande Anis) : moteur optionnel par IA de vision (via
# OpenRouter, même clé API que le module Factures) — utile pour les relevés scannés
# (aucune couche texte, donc pdfplumber ne peut rien lire) ou dans un format non
# reconnu par les parseurs dédiés/le parseur générique (ex. banques marocaines).
# Tenté en premier si OPENROUTER_API_KEY est renseignée ; en cas d'échec ou de résultat
# vide, repli automatique et silencieux sur les parseurs locaux existants — jamais de
# blocage de l'import pour une raison liée à l'IA.

import base64
import io
import json
from datetime import date
from pathlib import Path

from parsers.detector import detecter_parser
from parsers.base import Transaction
from services.facture_extractor import OPENROUTER_API_KEY, OPENROUTER_MODEL, OPENROUTER_TIMEOUT

PROMPT_IA_RELEVE = """Tu es un assistant d'extraction comptable. Cette image est une page d'un \
relevé bancaire. Extrait TOUTES les lignes de mouvement (transactions) visibles sur cette page \
sous forme d'un tableau JSON strict — réponds UNIQUEMENT avec un array JSON, sans texte autour \
ni bloc de code, chaque élément avec exactement ces champs :

{
  "date": "YYYY-MM-DD",
  "libelle": "libellé/description du mouvement",
  "debit": nombre ou null,
  "credit": nombre ou null,
  "solde": nombre ou null
}

Règles strictes :
- Une transaction bancaire a TOUJOURS soit un débit, soit un crédit — JAMAIS les deux à la fois.
  Ne remplis jamais "debit" et "credit" en même temps sur une même ligne.
- Chaque ligne du tableau source = un seul objet JSON. Ne fusionne JAMAIS le texte de deux
  lignes différentes dans un seul "libelle" — si deux lignes te semblent proches ou ambiguës,
  crée quand même deux objets JSON séparés plutôt que d'en fusionner le contenu.
- Recopie le libellé exactement tel qu'affiché (accents compris), sans le raccourcir ni le
  reformuler.
- "solde" = solde après ce mouvement si indiqué sur la ligne (sinon null).
- Les montants sont des nombres (point décimal, sans espace ni symbole monétaire).
- Ignore les lignes d'en-tête, de solde initial/final, de totaux récapitulatifs, de mentions
  légales. Si cette page ne contient aucun mouvement, réponds avec un array vide : [].
- N'invente jamais une ligne absente de l'image, et ne duplique jamais une ligne déjà extraite."""


def _extraire_json_liste(texte: str) -> list:
    debut, fin = texte.find("["), texte.rfind("]")
    if debut == -1 or fin == -1 or fin < debut:
        return []
    try:
        return json.loads(texte[debut:fin + 1])
    except json.JSONDecodeError:
        return []


def extraire_transactions_ia(chemin_pdf: str) -> list[Transaction]:
    """Extrait les transactions page par page via un modèle de vision (OpenRouter).
    Lève une exception sur tout échec réseau/clé — c'est l'appelant qui décide de
    retomber sur les parseurs locaux dans ce cas."""
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY non configurée")

    import httpx
    import pdfplumber

    nom_fichier = Path(chemin_pdf).name
    transactions: list[Transaction] = []

    with pdfplumber.open(chemin_pdf) as pdf:
        for page in pdf.pages:
            image = page.to_image(resolution=200).original
            buf = io.BytesIO()
            image.convert("RGB").save(buf, format="PNG")
            data_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

            reponse = httpx.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                    "X-Title": "Kwika Numerisation - releves",
                },
                json={
                    "model": OPENROUTER_MODEL,
                    "temperature": 0,
                    "messages": [
                        {"role": "user", "content": [
                            {"type": "text", "text": PROMPT_IA_RELEVE},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ]}
                    ],
                },
                timeout=OPENROUTER_TIMEOUT,
            )
            reponse.raise_for_status()
            contenu = reponse.json()["choices"][0]["message"]["content"]

            for ligne in _extraire_json_liste(contenu):
                try:
                    tx_date = date.fromisoformat(str(ligne.get("date"))[:10])
                except (ValueError, TypeError):
                    continue
                debit = ligne.get("debit")
                credit = ligne.get("credit")
                debit_rempli = debit not in ("", "null", None)
                credit_rempli = credit not in ("", "null", None)
                if not debit_rempli and not credit_rempli:
                    continue
                # Une transaction n'a jamais débit ET crédit à la fois — si les deux sont
                # remplis, c'est presque toujours deux lignes source fusionnées par erreur
                # par le modèle plutôt qu'un vrai mouvement double ; on l'écarte plutôt que
                # de garder une valeur dont on ne peut pas garantir qu'elle soit correcte.
                if debit_rempli and credit_rempli:
                    continue
                transactions.append(Transaction(
                    date=tx_date,
                    libelle=str(ligne.get("libelle") or "").strip(),
                    debit=float(debit) if debit not in ("", "null", None) else None,
                    credit=float(credit) if credit not in ("", "null", None) else None,
                    solde=float(ligne["solde"]) if ligne.get("solde") not in ("", "null", None) else None,
                    banque="IA (vision)",
                    fichier_source=nom_fichier,
                ))

    return transactions


PROMPT_IA_SOLDES = """Tu es un assistant d'extraction comptable. Ces images sont la première et la \
dernière page d'un relevé bancaire (parfois la même page si le relevé ne fait qu'une page). \
Trouve le solde initial (ancien solde, en début de période) et le solde final (nouveau solde, en \
fin de période) — réponds UNIQUEMENT avec un objet JSON strict, sans texte autour ni bloc de code :

{
  "solde_initial": nombre ou null,
  "solde_final": nombre ou null
}

Règles strictes :
- Un solde débiteur (le compte doit de l'argent) doit être un nombre NÉGATIF. Un solde créditeur
  est positif.
- Le nombre est au format point décimal, sans espace ni symbole monétaire (ex: 81317.06).
- Si un des deux soldes n'est pas visible sur les images fournies, mets null pour ce champ plutôt
  que d'inventer une valeur.
- N'extrais aucune ligne de mouvement, seulement ces deux soldes."""


def detecter_soldes_ia(chemin_pdf: str) -> dict:
    """Détecte le solde initial et le solde final d'un relevé via IA de vision (OpenRouter),
    évolution du 2026-09-22 (demande utilisateur : éviter de ressaisir le solde final à chaque
    import). Envoie la première et la dernière page en un seul appel. Retourne
    {"solde_initial": float|None, "solde_final": float|None} — jamais d'exception : en cas
    d'échec (pas de clé, erreur réseau, JSON invalide), retourne les deux à None pour que
    l'appelant laisse simplement les champs du formulaire vides plutôt que de bloquer l'import."""
    if not OPENROUTER_API_KEY:
        return {"solde_initial": None, "solde_final": None}

    import httpx
    import pdfplumber

    try:
        with pdfplumber.open(chemin_pdf) as pdf:
            pages_a_envoyer = [pdf.pages[0]]
            if len(pdf.pages) > 1:
                pages_a_envoyer.append(pdf.pages[-1])

            contenu_message = [{"type": "text", "text": PROMPT_IA_SOLDES}]
            for page in pages_a_envoyer:
                image = page.to_image(resolution=200).original
                buf = io.BytesIO()
                image.convert("RGB").save(buf, format="PNG")
                data_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
                contenu_message.append({"type": "image_url", "image_url": {"url": data_url}})

            reponse = httpx.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                    "X-Title": "Kikou Numerisation - soldes",
                },
                json={
                    "model": OPENROUTER_MODEL,
                    "temperature": 0,
                    "messages": [{"role": "user", "content": contenu_message}],
                },
                timeout=OPENROUTER_TIMEOUT,
            )
            reponse.raise_for_status()
            texte = reponse.json()["choices"][0]["message"]["content"]

            debut, fin = texte.find("{"), texte.rfind("}")
            if debut == -1 or fin == -1 or fin < debut:
                return {"solde_initial": None, "solde_final": None}
            data = json.loads(texte[debut:fin + 1])

            def _nombre(v):
                try:
                    return float(v) if v not in ("", "null", None) else None
                except (TypeError, ValueError):
                    return None

            return {"solde_initial": _nombre(data.get("solde_initial")), "solde_final": _nombre(data.get("solde_final"))}
    except Exception:
        return {"solde_initial": None, "solde_final": None}


def extraire_transactions(chemin_pdf: str) -> tuple[list[Transaction], str]:
    """
    Extrait les transactions d'un PDF de relevé bancaire.
    Retourne (transactions, nom_banque). Essaie d'abord les parseurs locaux dédiés par
    banque (gratuits, basés sur le texte réel du PDF — plus fiables que la vision par IA
    quand le format est reconnu) ; si aucun mouvement n'est trouvé (PDF scanné sans texte,
    format non reconnu même par le parseur générique), retombe sur l'IA de vision
    (OpenRouter) si une clé est configurée.
    """
    if not Path(chemin_pdf).exists():
        raise FileNotFoundError(f"Fichier introuvable : {chemin_pdf}")

    parser = detecter_parser(chemin_pdf)
    transactions = parser.parse(chemin_pdf)
    banque = parser.NOM_BANQUE

    if not transactions and OPENROUTER_API_KEY:
        try:
            transactions_ia = extraire_transactions_ia(chemin_pdf)
        except Exception:
            transactions_ia = []
        if transactions_ia:
            return transactions_ia, "IA (vision)"

    return transactions, banque
