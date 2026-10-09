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
import re
import io
import json
from datetime import date
from pathlib import Path

from parsers.detector import detecter_parser
from parsers.base import Transaction
import os

from services.facture_extractor import OPENROUTER_API_KEY, OPENROUTER_MODEL, OPENROUTER_TIMEOUT

# Modèle de vision essayé en second quand la lecture du modèle principal ne retrouve pas le
# solde final (vide dans .env = pas de second essai).
OPENROUTER_MODEL_SECOURS = os.environ.get("OPENROUTER_MODEL_SECOURS", "google/gemini-2.5-flash").strip()

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
- Un mouvement = UN montant imprimé dans la colonne débit ou crédit. Il doit y avoir exactement
  autant d'objets JSON que de montants imprimés dans ces colonnes (hors soldes et totaux) : ne
  fusionne jamais deux montants en un seul objet, et n'en saute aucun.
- Le libellé d'un mouvement peut s'étaler sur plusieurs lignes (ex. « PAIEMENT PRELEVEMENT »
  puis « LYDEC » juste en dessous, ou « VIREMENT RECU » puis le nom du donneur d'ordre) : les
  lignes de texte sans montant ni date qui suivent un mouvement font partie de son libellé —
  réunis-les avec un espace.
- Les titres de rubrique sans date ni montant (ex. « CHEQUES », « OPERATIONS DIVERSES »,
  « PAIEMENT PRELEVEMENTS », « VIREMENTS ET MISES A DISPOSITION ») ne sont pas des mouvements
  et ne font partie d'aucun libellé.
- « ANCIEN SOLDE », « SOLDE DEPART », « NOUVEAU SOLDE », « SOLDE FINAL », « TOTAL MOUVEMENTS »
  ne sont jamais des mouvements, même quand leur montant est imprimé dans la colonne crédit ou
  débit.
- Recopie le libellé exactement tel qu'affiché (accents compris), sans le raccourcir ni le
  reformuler.
- "solde" = solde après ce mouvement si indiqué sur la ligne (sinon null).
- Les montants sont des nombres (point décimal, sans espace ni symbole monétaire).
- Ignore les lignes d'en-tête, de solde initial/final, de totaux récapitulatifs, de mentions
  légales. Si cette page ne contient aucun mouvement, réponds avec un array vide : [].
- N'invente jamais une ligne absente de l'image, et ne duplique jamais une ligne déjà extraite.
- Ignore les annotations manuscrites (mots, coches, flèches, chiffres écrits à la main, ex.
  « LOYER », « CNSS ») : seuls le texte et les montants imprimés comptent, et rien d'écrit à la
  main ne doit apparaître dans un libellé.
- Sur les relevés scannés, les montants peuvent être légèrement décalés en hauteur par rapport
  aux libellés : rattache chaque montant imprimé à la ligne de mouvement la plus proche, en
  respectant l'ordre des lignes, et classe-le selon sa COLONNE (débit ou crédit), jamais selon
  le libellé.
- Certains relevés (ex. Attijariwafa) ont trois colonnes de montants : DÉBIT, CAPITAUX, CRÉDIT.
  Un montant imprimé sous DÉBIT ou CAPITAUX est un débit ; seul un montant dans la colonne la
  plus à droite (CRÉDIT) est un crédit.
- Les montants utilisent souvent un espace comme séparateur des milliers (ex. « 119 163,67 ») :
  recopie le nombre entier sans perdre de chiffre. Une coche ou flèche manuscrite devant un
  montant n'est pas un chiffre.
- Les dates sont au format jour mois année ; une année sur 2 chiffres se lit 20xx
  (ex. « 05 08 26 » = 2026-08-05). Utilise la date d'opération (première colonne de date).
- Si l'année n'est pas dans la colonne date, prends-la dans la colonne date de valeur ou dans
  la période du relevé."""


def _montant(valeur) -> float | None:
    """Convertit un montant renvoyé par l'IA (nombre, "15039.89", "15 039,89", "1.234,50"…)."""
    if valeur in ("", "null", None):
        return None
    if isinstance(valeur, (int, float)):
        return abs(float(valeur)) or None
    texte = str(valeur).replace("\u00a0", "").replace("\u202f", "").replace(" ", "")
    texte = texte.replace("DH", "").replace("MAD", "").replace("€", "").strip("+-")
    if "," in texte and "." in texte:
        texte = texte.replace(".", "").replace(",", ".") if texte.rfind(",") > texte.rfind(".") else texte.replace(",", "")
    elif "," in texte:
        texte = texte.replace(",", ".")
    try:
        return abs(float(texte)) or None
    except ValueError:
        return None


def _date(valeur) -> date | None:
    texte = str(valeur or "").strip()
    try:
        return date.fromisoformat(texte[:10])
    except ValueError:
        pass
    morceaux = [m for m in texte.replace("/", " ").replace("-", " ").replace(".", " ").split() if m.isdigit()]
    if len(morceaux) == 3:
        jour, mois, annee = (int(m) for m in morceaux)
        if annee < 100:
            annee += 2000
        try:
            return date(annee, mois, jour)
        except ValueError:
            return None
    return None


CODES_A_REESSAYER = {429, 500, 502, 503, 504}
ATTENTES_REESSAI = (5, 15, 30, 60)  # secondes entre les tentatives


def _appel_openrouter(httpx, **kwargs):
    """POST vers OpenRouter avec nouvelles tentatives sur 429 (limite de débit) et erreurs
    serveur temporaires, en respectant l'en-tête Retry-After quand il est fourni."""
    import time
    for attente in (*ATTENTES_REESSAI, None):
        reponse = httpx.post("https://openrouter.ai/api/v1/chat/completions", **kwargs)
        if reponse.status_code not in CODES_A_REESSAYER or attente is None:
            reponse.raise_for_status()
            return reponse
        try:
            attente = max(attente, min(float(reponse.headers.get("retry-after", 0)), 120))
        except ValueError:
            pass
        time.sleep(attente)


# Lignes de solde / report / total que l'IA (ou un parseur) renvoie parfois comme si c'étaient
# des mouvements — ex. « ANCIEN SOLDE AU 31/07/2026 » lu dans la colonne crédit, qui fausse le
# rapprochement du montant du solde initial. On les écarte quoi qu'il arrive.
PATTERN_LIGNE_SOLDE = re.compile(
    r"\b(ANCIEN|NOUVEAU|ANC\.?|NOUV\.?)\s+SOLDE\b"
    r"|^\s*SOLDE\s+(DE\s+)?(DEPART|DÉPART|INITIAL|FINAL|PRECEDENT|PRÉCÉDENT|REPORTE|REPORTÉ|AU\b|AU\s*\d|CREDITEUR|CRÉDITEUR|DEBITEUR|DÉBITEUR|DEBUT|DÉBUT|FIN)"
    r"|\bTOTAL\s+(DES\s+)?(MOUVEMENTS|OPERATIONS|OPÉRATIONS|DEBITS?|DÉBITS?|CREDITS?|CRÉDITS?)\b"
    r"|\b(REPORT|A\s+REPORTER|À\s+REPORTER|SOLDE\s+REPORTE|SOLDE\s+REPORTÉ)\s*$",
    re.IGNORECASE,
)


def _est_ligne_de_solde(libelle: str) -> bool:
    return bool(PATTERN_LIGNE_SOLDE.search(libelle or ""))


def _extraire_json_liste(texte: str) -> list:
    debut, fin = texte.find("["), texte.rfind("]")
    if debut == -1 or fin == -1 or fin < debut:
        return []
    try:
        return json.loads(texte[debut:fin + 1])
    except json.JSONDecodeError:
        return []


def extraire_transactions_ia(chemin_pdf: str, modele: str | None = None) -> list[Transaction]:
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

            reponse = _appel_openrouter(
                httpx,
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                    "X-Title": "Kwika Numerisation - releves",
                },
                json={
                    "model": modele or OPENROUTER_MODEL,
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
            contenu = reponse.json()["choices"][0]["message"]["content"]

            for ligne in _extraire_json_liste(contenu):
                if not isinstance(ligne, dict):
                    continue
                tx_date = _date(ligne.get("date"))
                if tx_date is None:
                    continue
                debit = _montant(ligne.get("debit"))
                credit = _montant(ligne.get("credit"))
                # Une transaction n'a jamais débit ET crédit à la fois — si les deux sont
                # remplis, c'est presque toujours deux lignes source fusionnées par erreur
                # par le modèle plutôt qu'un vrai mouvement double ; on l'écarte plutôt que
                # de garder une valeur dont on ne peut pas garantir qu'elle soit correcte.
                if (debit is None) == (credit is None):
                    continue
                libelle = str(ligne.get("libelle") or "").strip()
                if _est_ligne_de_solde(libelle):
                    continue
                transactions.append(Transaction(
                    date=tx_date,
                    libelle=libelle,
                    debit=debit,
                    credit=credit,
                    solde=_montant(ligne.get("solde")),
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

            reponse = _appel_openrouter(
                httpx,
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


def _message_erreur_ia(e: Exception) -> str:
    import httpx
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        if code == 401:
            return "clé API refusée par OpenRouter (401) — vérifiez OPENROUTER_API_KEY dans .env"
        if code == 402:
            return "crédits OpenRouter épuisés (402) — rechargez le compte sur openrouter.ai"
        if code == 429:
            return ("OpenRouter limite toujours les requêtes (429) après plusieurs tentatives — réessayez dans "
                    "quelques minutes, ou changez de modèle via OPENROUTER_MODEL dans .env")
        return f"erreur OpenRouter {code} : {e.response.text[:200]}"
    if isinstance(e, httpx.TimeoutException):
        return "délai dépassé en attendant l'IA — réessayez ou augmentez OPENROUTER_TIMEOUT dans .env"
    if isinstance(e, httpx.HTTPError):
        return f"connexion à OpenRouter impossible ({e.__class__.__name__})"
    return f"{e.__class__.__name__} : {e}"


def _ecart(transactions: list[Transaction], solde_initial, solde_final) -> float | None:
    if solde_initial is None or solde_final is None:
        return None
    total_debit = sum(t.debit or 0.0 for t in transactions)
    total_credit = sum(t.credit or 0.0 for t in transactions)
    return abs(round(solde_initial + total_credit - total_debit - solde_final, 2))


def extraire_transactions(
    chemin_pdf: str,
    moteur: str = "auto",
    solde_initial: float | None = None,
    solde_final: float | None = None,
    tolerance: float = 0.01,
) -> tuple[list[Transaction], str]:
    """
    Extrait les transactions d'un PDF de relevé bancaire.
    Retourne (transactions, nom_banque).

    moteur = "auto" : parseurs locaux d'abord (gratuits, fiables sur les PDF texte) ; l'IA de
      vision (OpenRouter) prend le relais si aucun mouvement n'est trouvé OU si les soldes
      fournis ne se rapprochent pas (cas typique des relevés scannés lus partiellement ou de
      travers par les parseurs locaux). On garde alors le résultat dont l'écart est le plus faible.
    moteur = "ia" : numérisation directe par l'IA (choix explicite de l'utilisateur) ; repli
      sur les parseurs locaux seulement si l'IA échoue ou ne renvoie rien.
    """
    if not Path(chemin_pdf).exists():
        raise FileNotFoundError(f"Fichier introuvable : {chemin_pdf}")

    erreur_ia = None

    def _essayer_ia(modele: str | None = None) -> list[Transaction]:
        nonlocal erreur_ia
        try:
            resultat = extraire_transactions_ia(chemin_pdf, modele)
        except Exception as e:
            erreur_ia = _message_erreur_ia(e)
            return []
        if not resultat:
            erreur_ia = "l'IA n'a renvoyé aucune ligne de mouvement exploitable"
        return resultat

    def _rapproche(candidat: list[Transaction]) -> bool:
        ecart = _ecart(candidat, solde_initial, solde_final)
        return bool(candidat) and (ecart is None or ecart <= tolerance)

    def _meilleur(*candidats: tuple[list[Transaction], str]) -> tuple[list[Transaction], str]:
        """Résultat non vide dont l'écart de solde est le plus faible (le premier à égalité)."""
        non_vides = [c for c in candidats if c[0]]
        if not non_vides:
            return [], ""
        return min(non_vides, key=lambda c: _ecart(c[0], solde_initial, solde_final) or 0.0)

    def _ia_avec_secours() -> list[Transaction]:
        resultat = _essayer_ia()
        if _rapproche(resultat) or not OPENROUTER_MODEL_SECOURS or OPENROUTER_MODEL_SECOURS == OPENROUTER_MODEL:
            return resultat
        secours = _essayer_ia(OPENROUTER_MODEL_SECOURS)
        return _meilleur((resultat, ""), (secours, ""))[0]

    if moteur == "ia":
        if not OPENROUTER_API_KEY:
            raise RuntimeError("Clé API OpenRouter absente : renseignez OPENROUTER_API_KEY dans le fichier .env")
        transactions_ia = _ia_avec_secours()
        if transactions_ia:
            return transactions_ia, "IA (vision)"

    parser = detecter_parser(chemin_pdf)
    transactions = [t for t in parser.parse(chemin_pdf) if not _est_ligne_de_solde(t.libelle)]
    banque = parser.NOM_BANQUE

    if moteur == "ia" or not OPENROUTER_API_KEY:
        if not transactions and erreur_ia:
            raise RuntimeError(f"Numérisation par IA impossible : {erreur_ia}")
        return transactions, banque

    if _rapproche(transactions):
        return transactions, banque

    transactions_ia = _ia_avec_secours()
    if not transactions_ia and not transactions:
        raise RuntimeError(f"PDF non lisible localement (scan) et numérisation par IA impossible : {erreur_ia}")
    return _meilleur((transactions, banque), (transactions_ia, "IA (vision)"))

    ecart_local = _ecart(transactions, solde_initial, solde_final)
    if transactions and (ecart_local is None or ecart_local <= tolerance):
        return transactions, banque

    transactions_ia = _essayer_ia()
    if not transactions_ia:
        if not transactions:
            raise RuntimeError(f"PDF non lisible localement (scan) et numérisation par IA impossible : {erreur_ia}")
        return transactions, banque
    if not transactions:
        return transactions_ia, "IA (vision)"

    ecart_ia = _ecart(transactions_ia, solde_initial, solde_final)
    if ecart_ia is not None and ecart_ia < ecart_local:
        return transactions_ia, "IA (vision)"
    return transactions, banque
