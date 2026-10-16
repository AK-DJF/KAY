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
from datetime import date, datetime
from pathlib import Path

from parsers.detector import detecter_parser
from parsers.base import Transaction
import os

from services.facture_extractor import OPENROUTER_API_KEY, OPENROUTER_MODEL, OPENROUTER_TIMEOUT

# Modèle de vision essayé en second quand la lecture du modèle principal ne retrouve pas le
# solde final (vide dans .env = pas de second essai).
PASSES_VERIFICATION = int(os.environ.get("OPENROUTER_PASSES_VERIFICATION", "2"))
PAGES_MAX_VERIFICATION = 15  # au-delà, la requête de vérification (toutes les pages) devient trop lourde
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
- « ANCIEN SOLDE », « SOLDE DEPART », « NOUVEAU SOLDE », « SOLDE FINAL », « TOTAL MOUVEMENTS »,
  et les reports entre pages « SOLDE REPORT », « SOLDE A REPORTER », « A REPORTER »,
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


def _date(valeur, periode: tuple[int, int] | None = None) -> date | None:
    """Date renvoyée par l'IA ou un tableur : ISO, JJ/MM/AAAA, JJ MM AA, ou JJ/MM sans année
    (fréquent sur les relevés marocains) complétée avec la période du relevé."""
    texte = str(valeur or "").strip()
    try:
        return date.fromisoformat(texte[:10])
    except ValueError:
        pass
    morceaux = [m for m in re.split(r"[\s/.\-]+", texte) if m.isdigit()]
    if len(morceaux) >= 3:
        if len(morceaux[0]) == 4:  # AAAA MM JJ
            annee, mois, jour = (int(m) for m in morceaux[:3])
        else:
            jour, mois, annee = (int(m) for m in morceaux[:3])
        if annee < 100:
            annee += 2000
    elif len(morceaux) == 2 and periode:
        jour, mois = (int(m) for m in morceaux)
        annee = periode[0]
        # Relevé de janvier : une opération de décembre appartient à l'année précédente.
        if mois == 12 and periode[1] == 1:
            annee -= 1
    else:
        return None
    try:
        return date(annee, mois, jour)
    except ValueError:
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
    r"|^\s*SOLDE\s+(DE\s+)?(DEPART|DÉPART|INITIAL|FINAL|PRECEDENT|PRÉCÉDENT|REPORTE|REPORTÉ|REPORT\b|A\s+REPORTER|À\s+REPORTER|AU\b|AU\s*\d|CREDITEUR|CRÉDITEUR|DEBITEUR|DÉBITEUR|DEBUT|DÉBUT|FIN)"
    r"|\bTOTAL\s+(DES\s+)?(MOUVEMENTS|OPERATIONS|OPÉRATIONS|DEBITS?|DÉBITS?|CREDITS?|CRÉDITS?)\b"
    r"|\b(REPORT|A\s+REPORTER|À\s+REPORTER|SOLDE\s+REPORTE|SOLDE\s+REPORTÉ)[\s:.\-]*$"
    r"|^[\s:.\-]*(REPORT|A\s+REPORTER|À\s+REPORTER)\b",
    re.IGNORECASE,
)


def _est_ligne_de_solde(libelle: str) -> bool:
    return bool(PATTERN_LIGNE_SOLDE.search(libelle or ""))


def _charger_json_souple(texte: str):
    try:
        return json.loads(texte)
    except json.JSONDecodeError:
        # Virgules finales ou blocs ```json : erreurs de format fréquentes des modèles.
        nettoye = re.sub(r",\s*([\]}])", r"\1", texte)
        return json.loads(nettoye)


def _extraire_json_liste(texte: str) -> list:
    """Liste de mouvements dans la réponse du modèle : array JSON nu, entouré de texte ou d'un
    bloc ```json, ou rangé dans un objet ({"transactions": [...]}, {"mouvements": [...]}…)."""
    texte = (texte or "").strip()
    texte = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", texte)
    candidats = [texte]
    debut, fin = texte.find("["), texte.rfind("]")
    if debut != -1 and fin > debut:
        candidats.append(texte[debut:fin + 1])
    debut, fin = texte.find("{"), texte.rfind("}")
    if debut != -1 and fin > debut:
        candidats.append(texte[debut:fin + 1])
    for candidat in candidats:
        try:
            donnees = _charger_json_souple(candidat)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(donnees, list):
            return donnees
        if isinstance(donnees, dict):
            listes = [v for v in donnees.values() if isinstance(v, list) and any(isinstance(x, dict) for x in v)]
            if listes:
                return max(listes, key=len)
            if any(k in _cles_normalisees(donnees) for k in ("date", "debit", "credit", "montant")):
                return [donnees]
    return []


def _sans_accents(texte: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode().lower()


def _cles_normalisees(ligne: dict) -> dict:
    """Clés en minuscules sans accents ni séparateurs : « Débit », « date_operation »,
    « Date Opération » deviennent « debit », « dateoperation »…"""
    return {re.sub(r"[^a-z]", "", _sans_accents(str(k))): v for k, v in ligne.items()}


def _premiere_valeur(ligne: dict, *cles: str):
    for cle in cles:
        if ligne.get(cle) not in (None, "", "null"):
            return ligne[cle]
    return None


COTE_MAX_IMAGE = 2400  # px : au-delà, l'image est réduite (lisibilité suffisante, requête plus légère)


def _pages_pil(chemin_pdf: str, resolution: int = 200) -> list:
    """Pages du document en images PIL. Essaie pypdfium2 (tolérant aux PDF de scanners et
    d'applications mobiles mal formés), puis pdfplumber ; accepte aussi une image (JPEG/PNG)
    enregistrée avec l'extension .pdf."""
    from PIL import Image

    with open(chemin_pdf, "rb") as f:
        entete = f.read(8)
    if entete.startswith(b"\xff\xd8") or entete.startswith(b"\x89PNG"):
        return [Image.open(chemin_pdf).convert("RGB")]

    pages = []
    try:
        import pypdfium2 as pdfium
        document = pdfium.PdfDocument(chemin_pdf)
        try:
            for index in range(len(document)):
                pages.append(document[index].render(scale=resolution / 72).to_pil().convert("RGB"))
        finally:
            document.close()
    except Exception:
        pages = []
    if not pages:
        try:
            import pdfplumber
            with pdfplumber.open(chemin_pdf) as pdf:
                pages = [p.to_image(resolution=resolution).original.convert("RGB") for p in pdf.pages]
        except Exception:
            pages = []
    return pages


def _images_pdf(chemin_pdf: str, resolution: int = 200) -> list[str]:
    """Pages du PDF en images JPEG encodées en data URL (pour les modèles de vision)."""
    images = []
    for image in _pages_pil(chemin_pdf, resolution):
        if max(image.size) > COTE_MAX_IMAGE:
            image.thumbnail((COTE_MAX_IMAGE, COTE_MAX_IMAGE))
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=88)
        images.append("data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii"))
    return images


ERREUR_AUCUNE_PAGE = ("aucune page lisible dans le PDF — il est peut-être protégé par un mot de passe ou "
                      "endommagé : ouvrez-le puis « Imprimer » > « Microsoft Print to PDF » pour en refaire "
                      "une copie, et importez cette copie")


def _demander_ia(contenu: list[dict], modele: str | None, titre: str) -> str:
    import httpx
    reponse = _appel_openrouter(
        httpx,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "X-Title": titre,
        },
        json={
            "model": modele or OPENROUTER_MODEL,
            "temperature": 0,
            "messages": [{"role": "user", "content": contenu}],
        },
        timeout=OPENROUTER_TIMEOUT,
    )
    contenu = reponse.json()["choices"][0]["message"].get("content")
    if isinstance(contenu, list):  # certains fournisseurs renvoient des « parts »
        contenu = "".join(p.get("text", "") for p in contenu if isinstance(p, dict))
    contenu = contenu or ""
    _journaliser_reponse(titre, modele or OPENROUTER_MODEL, contenu)
    return contenu


# Dernières réponses brutes de l'IA, gardées dans ia_reponses.log (dossier de l'application)
# pour diagnostiquer une lecture vide ou fausse sans avoir à reproduire l'appel.
JOURNAL_IA = Path(__file__).resolve().parent.parent / "ia_reponses.log"
_derniere_reponse_ia = ""


def _journaliser_reponse(titre: str, modele: str, contenu: str):
    global _derniere_reponse_ia
    _derniere_reponse_ia = contenu
    try:
        with open(JOURNAL_IA, "a", encoding="utf-8") as f:
            f.write(f"===== {datetime.now():%Y-%m-%d %H:%M:%S} | {titre} | {modele}\n{contenu}\n\n")
    except OSError:
        pass


def _vider_journal_ia():
    try:
        JOURNAL_IA.write_text("", encoding="utf-8")
    except OSError:
        pass


def _transactions_depuis_json(lignes: list, nom_fichier: str, periode: tuple[int, int] | None = None) -> list[Transaction]:
    transactions = []
    for brute in lignes:
        if not isinstance(brute, dict):
            continue
        ligne = _cles_normalisees(brute)
        tx_date = _date(_premiere_valeur(ligne, "date", "dateoperation", "dateop", "datevaleur", "valeur"), periode)
        if tx_date is None:
            continue
        debit = _montant(_premiere_valeur(ligne, "debit", "debits", "montantdebit", "sortie", "retrait"))
        credit = _montant(_premiere_valeur(ligne, "credit", "credits", "montantcredit", "entree", "versement"))
        if debit is None and credit is None and _premiere_valeur(ligne, "montant", "amount") is not None:
            # Format { "montant": …, "sens": "debit" | "credit" } ou montant signé.
            brut = _premiere_valeur(ligne, "montant", "amount")
            montant = _montant(brut)
            sens = _sans_accents(str(_premiere_valeur(ligne, "sens", "type", "nature") or ""))
            negatif = str(brut).strip().startswith("-") or (isinstance(brut, (int, float)) and brut < 0)
            if "deb" in sens or (not sens and negatif):
                debit = montant
            elif "cred" in sens or not sens:
                credit = montant
        # Une transaction n'a jamais débit ET crédit à la fois — si les deux sont
        # remplis, c'est presque toujours deux lignes source fusionnées par erreur
        # par le modèle plutôt qu'un vrai mouvement double ; on l'écarte plutôt que
        # de garder une valeur dont on ne peut pas garantir qu'elle soit correcte.
        if (debit is None) == (credit is None):
            continue
        libelle = str(_premiere_valeur(ligne, "libelle", "description", "label", "operation", "designation") or "").strip()
        if _est_ligne_de_solde(libelle):
            continue
        transactions.append(Transaction(
            date=tx_date,
            libelle=libelle,
            debit=debit,
            credit=credit,
            solde=_montant(_premiere_valeur(ligne, "solde", "balance")),
            banque="IA (vision)",
            fichier_source=nom_fichier,
        ))
    return transactions


def extraire_transactions_ia(chemin_pdf: str, modele: str | None = None, periode: tuple[int, int] | None = None) -> list[Transaction]:
    """Extrait les transactions page par page via un modèle de vision (OpenRouter).
    Lève une exception sur tout échec réseau/clé — c'est l'appelant qui décide de
    retomber sur les parseurs locaux dans ce cas."""
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY non configurée")

    nom_fichier = Path(chemin_pdf).name
    transactions: list[Transaction] = []
    images = _images_pdf(chemin_pdf)
    if not images:
        raise RuntimeError(ERREUR_AUCUNE_PAGE)
    for data_url in images:
        contenu = _demander_ia(
            [{"type": "text", "text": PROMPT_IA_RELEVE}, {"type": "image_url", "image_url": {"url": data_url}}],
            modele, "Kikou Numerisation - releves",
        )
        transactions.extend(_transactions_depuis_json(_extraire_json_liste(contenu), nom_fichier, periode))
    return transactions


PROMPT_IA_CORRECTION = """Tu as extrait les mouvements ci-dessous du relevé bancaire joint (toutes \
les pages, dans l'ordre). Le contrôle de solde ne tombe pas juste :

  solde initial {solde_initial:.2f} + crédits extraits {total_credit:.2f} − débits extraits \
{total_debit:.2f} = {solde_calcule:.2f}
  solde final imprimé sur le relevé : {solde_final:.2f}
  écart : {ecart:+.2f} (le calcul est trop {sens})

Au moins une ligne est donc fausse : mouvement oublié, ligne en double, montant mal lu (chiffre \
manquant ou en trop, séparateur de milliers), montant classé dans la mauvaise colonne (débit au \
lieu de crédit ou l'inverse), ou ligne de solde / de total prise pour un mouvement. Si le relevé \
imprime des totaux (ex. TOTAL MOUVEMENTS, total débit, total crédit), compare-les aux totaux \
extraits ci-dessus pour savoir de quel côté se trouve l'erreur.

Relis attentivement les images, ligne par ligne, et renvoie la liste COMPLÈTE et corrigée de \
tous les mouvements du relevé (pas seulement les lignes modifiées), au même format JSON strict : \
un array d'objets {{"date": "YYYY-MM-DD", "libelle": ..., "debit": nombre ou null, "credit": \
nombre ou null}}, sans texte autour. Ne modifie pas une ligne correcte, et n'invente jamais une \
ligne absente des images pour forcer le solde.

Mouvements extraits :
{lignes}"""


def corriger_transactions_ia(
    chemin_pdf: str,
    transactions: list[Transaction],
    solde_initial: float,
    solde_final: float,
    modele: str | None = None,
    images: list[str] | None = None,
    periode: tuple[int, int] | None = None,
) -> list[Transaction]:
    """Deuxième passe de vérification : renvoie au modèle les pages, sa première lecture et
    l'écart de solde constaté, pour qu'il corrige lui-même les lignes fausses."""
    total_debit = round(sum(t.debit or 0.0 for t in transactions), 2)
    total_credit = round(sum(t.credit or 0.0 for t in transactions), 2)
    solde_calcule = round(solde_initial + total_credit - total_debit, 2)
    ecart = round(solde_calcule - solde_final, 2)
    lignes = json.dumps(
        [{"date": t.date.isoformat(), "libelle": t.libelle, "debit": t.debit, "credit": t.credit} for t in transactions],
        ensure_ascii=False, indent=0,
    )
    texte = PROMPT_IA_CORRECTION.format(
        solde_initial=solde_initial, total_credit=total_credit, total_debit=total_debit,
        solde_calcule=solde_calcule, solde_final=solde_final, ecart=ecart,
        sens="haut" if ecart > 0 else "bas", lignes=lignes,
    )
    contenu = [{"type": "text", "text": texte}]
    contenu += [{"type": "image_url", "image_url": {"url": u}} for u in (images or _images_pdf(chemin_pdf))]
    reponse = _demander_ia(contenu, modele, "Kikou Numerisation - verification releve")
    return _transactions_depuis_json(_extraire_json_liste(reponse), Path(chemin_pdf).name, periode)


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

    try:
        images = _images_pdf(chemin_pdf)
        if not images:
            return {"solde_initial": None, "solde_final": None}
        contenu_message = [{"type": "text", "text": PROMPT_IA_SOLDES}]
        for data_url in ([images[0], images[-1]] if len(images) > 1 else images):
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
        if code in (400, 404) and "model" in e.response.text.lower():
            return f"modèle « {OPENROUTER_MODEL} » inconnu ou indisponible sur OpenRouter — corrigez OPENROUTER_MODEL dans .env"
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
    if isinstance(e, RuntimeError):
        return str(e)
    return f"{e.__class__.__name__} : {e}"


def _recaler_annees(transactions: list[Transaction], periode: tuple[int, int] | None) -> list[Transaction]:
    """Les parseurs locaux devinent l'année à partir du premier « 20xx » du texte, qui peut être
    un numéro de compte ou une référence (ex. 2009) ; l'IA peut aussi se tromper d'année. On
    recale toute date dont l'année s'écarte de plus d'un an de la période choisie à l'import,
    en gardant jour et mois (décembre d'un relevé de janvier -> année précédente, et inversement)."""
    if not periode:
        return transactions
    annee, mois = periode
    for t in transactions:
        if abs(t.date.year - annee) <= 1:
            continue
        nouvelle = annee - 1 if (t.date.month == 12 and mois == 1) else annee + 1 if (t.date.month == 1 and mois == 12) else annee
        try:
            t.date = t.date.replace(year=nouvelle)
        except ValueError:  # 29 février
            t.date = t.date.replace(year=nouvelle, day=28)
        t.annee, t.mois = t.date.year, t.date.month
    return transactions


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
    infos: dict | None = None,
    periode: tuple[int, int] | None = None,
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

    global _derniere_reponse_ia
    _vider_journal_ia()
    _derniere_reponse_ia = ""
    infos = infos if infos is not None else {}
    infos.update(ia_utilisee=False, erreur_ia=None)
    erreur_ia = None

    def _essayer_ia(modele: str | None = None) -> list[Transaction]:
        nonlocal erreur_ia
        infos["ia_utilisee"] = True
        try:
            resultat = _recaler_annees(extraire_transactions_ia(chemin_pdf, modele, periode), periode)
        except Exception as e:
            erreur_ia = _message_erreur_ia(e)
            infos["erreur_ia"] = erreur_ia
            return []
        if not resultat:
            extrait = " ".join(_derniere_reponse_ia.split())[:250] or "(réponse vide)"
            erreur_ia = (f"l'IA n'a renvoyé aucune ligne de mouvement exploitable — début de sa réponse : « {extrait} » "
                         f"(réponses complètes dans {JOURNAL_IA.name})")
            infos["erreur_ia"] = erreur_ia
        else:
            infos["erreur_ia"] = None
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

    def _verifier(resultat: list[Transaction]) -> list[Transaction]:
        """Passes de vérification par l'IA tant que le solde ne tombe pas juste ; on ne garde
        une correction que si elle réduit l'écart."""
        if not resultat or solde_initial is None or solde_final is None:
            return resultat
        try:
            images = _images_pdf(chemin_pdf)
        except Exception:
            return resultat
        if len(images) > PAGES_MAX_VERIFICATION:
            return resultat
        for _ in range(PASSES_VERIFICATION):
            if _rapproche(resultat):
                break
            try:
                corrige = corriger_transactions_ia(chemin_pdf, resultat, solde_initial, solde_final, images=images, periode=periode)
            except Exception:
                break
            corrige = _recaler_annees(corrige, periode)
            if not corrige or _ecart(corrige, solde_initial, solde_final) >= _ecart(resultat, solde_initial, solde_final):
                break
            resultat = corrige
        return resultat

    def _ia_avec_secours() -> list[Transaction]:
        resultat = _verifier(_essayer_ia())
        if _rapproche(resultat) or not OPENROUTER_MODEL_SECOURS or OPENROUTER_MODEL_SECOURS == OPENROUTER_MODEL:
            return resultat
        secours = _essayer_ia(OPENROUTER_MODEL_SECOURS)
        return _meilleur((resultat, ""), (secours, ""))[0]

    if moteur == "ia" and not OPENROUTER_API_KEY:
        raise RuntimeError("Clé API OpenRouter absente : renseignez OPENROUTER_API_KEY dans le fichier .env")

    parser = detecter_parser(chemin_pdf)
    try:
        transactions = _recaler_annees(
            [t for t in parser.parse(chemin_pdf) if not _est_ligne_de_solde(t.libelle)], periode
        )
    except Exception:
        # PDF que pdfplumber ne sait pas ouvrir (scan mal formé, image renommée…) : l'IA, qui
        # passe par un autre moteur de rendu, peut encore le lire.
        transactions = []
    banque = parser.NOM_BANQUE

    if not OPENROUTER_API_KEY:
        infos["erreur_ia"] = "aucune clé API OpenRouter dans le fichier .env"
        return transactions, banque

    if moteur == "ia":
        # PDF texte lu exactement par un parseur dédié : inutile (et plus risqué) de payer l'IA.
        if _rapproche(transactions) and solde_final is not None:
            return transactions, banque
        transactions_ia = _ia_avec_secours()
        if not transactions_ia and not transactions:
            raise RuntimeError(f"Numérisation par IA impossible : {erreur_ia}")
        return _meilleur((transactions_ia, "IA (vision)"), (transactions, banque))

    if _rapproche(transactions):
        return transactions, banque

    transactions_ia = _ia_avec_secours()
    if not transactions_ia and not transactions:
        raise RuntimeError(f"PDF non lisible localement (scan) et numérisation par IA impossible : {erreur_ia}")
    return _meilleur((transactions, banque), (transactions_ia, "IA (vision)"))


def tester_cle_ia() -> dict:
    """Petit appel texte à OpenRouter pour vérifier la clé, les crédits et le nom du modèle,
    sans attendre l'import d'un relevé."""
    if not OPENROUTER_API_KEY:
        return {"ok": False, "modele": OPENROUTER_MODEL, "message": "aucune clé API OpenRouter dans le fichier .env"}
    try:
        _demander_ia([{"type": "text", "text": "Réponds uniquement : OK"}], None, "Kikou Numerisation - test cle")
    except Exception as e:
        return {"ok": False, "modele": OPENROUTER_MODEL, "message": _message_erreur_ia(e)}
    return {"ok": True, "modele": OPENROUTER_MODEL, "message": "clé API et modèle opérationnels"}
