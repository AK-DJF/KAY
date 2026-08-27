# facture_extractor.py
# Lecture d'une facture (achat ou vente) par extraction de texte + reconnaissance de
# motifs — même outil que le module relevés bancaires (pdfplumber, local, gratuit),
# pas d'IA de vision ni de clé API. Une facture arrive avec un layout différent à
# chaque fournisseur/client (contrairement aux relevés, qui n'ont qu'une poignée de
# banques à calibrer) : pas de parseur dédié par tiers, une seule méthodologie
# générique par mots-clés, à affiner une fois de vrais exemples fournis.
#
# Méthodologie (définie avec Anis le 2026-08-08) :
# - Numéro de facture : précédé du mot "facture", "invoice" ou "référence"/"ref"
# - Date : généralement en haut du document (zone haute de la page, gauche ou droite)
# - Montants HT/TVA/TTC : sur la ligne contenant le mot-clé correspondant
# - Sujet de la facture : ligne "Objet"/"Désignation"/"Description" si présente
# - Nom du tiers détecté : lignes d'en-tête et de pied de page (aide au rapprochement
#   avec la liste déroulante de tiers, ne remplace jamais la sélection manuelle)
#
# Extension du 2026-08-16 (demande Anis) : les images (scan/photo de facture, sans
# PDF) passent par RapidOCR — moteur OCR local en ONNX, gratuit, sans clé API, dans
# le même esprit que pdfplumber pour les PDF. Chaque bloc de texte détecté par l'OCR
# est converti au même format que pdfplumber.extract_words() (text/x0/x1/top), pour
# réutiliser telles quelles les heuristiques de repérage écrites pour les PDF. Un PDF
# scanné (sans couche texte) suit le même chemin OCR : la page est rasterisée en
# image (pdfplumber/pypdfium2) avant d'être passée à RapidOCR.
#
# Extension du 2026-08-20 (copie de travail OpenRouter) : moteur optionnel par IA de
# vision (via OpenRouter, clé API dans .env) tenté en premier si OPENROUTER_API_KEY
# est renseignée — un seul appel image -> JSON structuré, potentiellement plus fiable
# que les heuristiques par mots-clés sur des mises en page variées. Sans clé configurée,
# ou si l'appel échoue (réseau, quota, réponse invalide...), repli automatique et
# silencieux sur le moteur local existant (pdfplumber/RapidOCR) — jamais de blocage de
# l'import pour une raison liée à l'IA.

import base64
import io
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pdfplumber
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent))
from parsers.base import regrouper_lignes_par_position

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "mistralai/mistral-small-2603").strip()
OPENROUTER_TIMEOUT = float(os.environ.get("OPENROUTER_TIMEOUT", "60"))

MOTS_CLES_NUMERO = r"(?:facture|invoice|r[ée]f[ée]rence|ref)\.?[ \t]*n?[o°]?\.?[ \t]*[:#]?[ \t]*"
# Le jeton capturé doit contenir au moins un chiffre (lookahead) — un numéro de facture réel en a
# toujours un ; ça écarte les faux positifs du type "Facture acquittée" ou "Facture" suivi d'un nom
# de société en toutes lettres (trouvé le 2026-08-16 sur de vraies factures).
RE_NUMERO = re.compile(MOTS_CLES_NUMERO + r"(?=[A-Z0-9\-/\._]*\d)([A-Z0-9][A-Z0-9\-/\._]{2,})", re.IGNORECASE)
# Repli : certaines factures étiquettent le numéro juste "Numéro : XXX", sans répéter "facture"
# avant — le ":" immédiatement après "numéro" évite de confondre avec "Numéro de TVA/client/compte".
RE_NUMERO_ALT = re.compile(r"num[ée]ro[ \t]*[:#][ \t]*(?=[A-Z0-9\-/\._]*\d)([A-Z0-9][A-Z0-9\-/\._]{2,})", re.IGNORECASE)

RE_DATE = re.compile(r"\b(\d{1,2})[\/\-\.](\d{1,2})[\/\-\.](\d{2,4})\b")

MOTS_CLES_HT = ("total ht", "montant ht", "sous-total", "sous total", " ht")
MOTS_CLES_TVA = ("tva",)
# Lignes contenant "tva" à écarter du repérage de montant — trouvées le 2026-08-16 sur de vraies
# factures où elles étaient prises à tort pour la ligne de montant :
# - "intra" : identifiant de TVA intracommunautaire de l'émetteur (ex. "N° de TVA Intra. : FR...")
# - "sur les" : mention légale du régime de TVA ("TVA sur les débits"/"sur les encaissements"),
#   pas un montant, mais contient parfois un chiffre parasite (ex. "1 mois offert").
EXCLUSIONS_MONTANT_TVA = ("intra", "sur les")
MOTS_CLES_TTC = ("total ttc", "montant ttc", "net à payer", "net a payer", " ttc")

MOTS_CLES_SUJET = ("objet", "désignation", "designation", "description", "concernant")

EXTENSIONS_IMAGE = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}


class ExtractionError(Exception):
    pass


def _normaliser_nombre(texte: str) -> float | None:
    """Convertit '1 234,56', '1234.56', '1.234,56' etc. en float."""
    t = texte.strip().replace(" ", "").replace(" ", "")
    t = re.sub(r"[^\d,.\-]", "", t)
    if not t:
        return None
    if "," in t and "." in t:
        # Le dernier séparateur rencontré est le séparateur décimal
        if t.rfind(",") > t.rfind("."):
            t = t.replace(".", "").replace(",", ".")
        else:
            t = t.replace(",", "")
    elif "," in t:
        t = t.replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def _chercher_montant(
    lignes: list[str], mots_cles: tuple[str, ...], exclure: tuple[str, ...] = ()
) -> float | None:
    """Cherche la 1ère ligne contenant l'un des mots-clés (et aucun des motifs d'exclusion),
    retourne le dernier nombre trouvé sur cette ligne (les montants sont généralement en fin
    de ligne, alignés à droite sur une facture)."""
    for ligne in lignes:
        bas = ligne.lower()
        if any(mc in bas for mc in mots_cles) and not any(ex in bas for ex in exclure):
            nombres = re.findall(r"[\d][\d\s ]*[.,]?\d*", ligne)
            for candidat in reversed(nombres):
                val = _normaliser_nombre(candidat)
                if val is not None and val > 0:
                    return val
    return None


def _chercher_numero(texte: str) -> str | None:
    m = RE_NUMERO.search(texte)
    if m:
        return m.group(1).strip()
    m = RE_NUMERO_ALT.search(texte)
    return m.group(1).strip() if m else None


def _chercher_date(mots: list[dict], hauteur: float) -> date | None:
    """Cherche une date dans le tiers supérieur du document (généralement en haut,
    à gauche ou à droite), sinon retombe sur la première date trouvée dans tout
    le document. `mots` suit le format pdfplumber.extract_words() (text/x0/x1/top),
    que la source soit un PDF ou une image passée par OCR."""
    zone_haute = [w for w in mots if w["top"] < hauteur * 0.30]

    for source in (zone_haute, mots):
        texte_zone = " ".join(w["text"] for w in source)
        m = RE_DATE.search(texte_zone)
        if m:
            j, mo, a = m.groups()
            if len(a) == 2:
                a = "20" + a
            try:
                return date(int(a), int(mo), int(j))
            except ValueError:
                continue
    return None


def _chercher_sujet(lignes: list[str]) -> str | None:
    for i, ligne in enumerate(lignes):
        bas = ligne.lower().strip()
        for mc in MOTS_CLES_SUJET:
            if bas.startswith(mc):
                reste = ligne.split(":", 1)
                if len(reste) > 1 and reste[1].strip():
                    return reste[1].strip()
                # Le libellé est seul sur sa ligne -> la valeur est probablement juste après
                if i + 1 < len(lignes) and lignes[i + 1].strip():
                    return lignes[i + 1].strip()
    return None


# Blocs à écarter du repérage du nom de tiers (2026-08-16, vraies factures) :
# - titres génériques du document (le mot "FACTURE" seul en haut de page, pas un nom d'émetteur)
# - en-tête d'audit DocuSign ("DocuSign Envelope ID: ...", ajouté par la signature électronique,
#   littéralement le tout premier bloc de texte sur un PDF signé — avant le vrai en-tête facture)
TITRES_GENERIQUES_NOM_TIERS = ("facture", "invoice", "devis", "quote")
EXCLUSIONS_NOM_TIERS = ("docusign",)


def _nom_tiers_detecte(mots: list[dict]) -> str | None:
    """Premier bloc de texte en haut à gauche du document = généralement le nom de
    l'émetteur (en-tête). Purement indicatif, à rapprocher manuellement de la liste
    de tiers. Regroupe par position (pas par extract_text() brut) pour ne pas fusionner
    ce bloc avec un autre élément placé sur la même ligne visuelle (ex. la date, souvent
    en haut à droite sur la même hauteur que le nom de l'émetteur) — cf. `parsers/base.py`,
    déjà utilisé pour ce même besoin sur les relevés Société Générale/BRED."""
    if not mots:
        return None
    lignes_pos = regrouper_lignes_par_position(mots)
    for ligne in lignes_pos:
        bloc: list[str] = []
        x_precedent = None
        for mot in ligne:
            if RE_DATE.fullmatch(mot["text"]) or re.fullmatch(r"\d[\d\/\-\.]*", mot["text"]):
                break  # un jeton numérique/date marque la fin du bloc "nom"
            if x_precedent is not None and mot["x0"] - x_precedent > 25:
                break  # grand espace horizontal = nouvelle colonne (ex. date à droite)
            bloc.append(mot["text"])
            x_precedent = mot["x1"]
        texte_bloc = " ".join(bloc).strip()
        bas = texte_bloc.lower()
        if len(texte_bloc) > 2 and bas not in TITRES_GENERIQUES_NOM_TIERS and not any(ex in bas for ex in EXCLUSIONS_NOM_TIERS):
            return texte_bloc
    return None


def _construire_resultat(mots: list[dict], hauteur: float, texte: str) -> dict:
    lignes = [l for l in texte.split("\n") if l.strip()]
    return {
        "date": _chercher_date(mots, hauteur),
        "numero": _chercher_numero(texte),
        "montant_ht": _chercher_montant(lignes, MOTS_CLES_HT),
        "montant_tva": _chercher_montant(lignes, MOTS_CLES_TVA, exclure=EXCLUSIONS_MONTANT_TVA),
        "montant_ttc": _chercher_montant(lignes, MOTS_CLES_TTC),
        "sujet_detecte": _chercher_sujet(lignes),
        "nom_tiers_detecte": _nom_tiers_detecte(mots),
    }


def _lire_pdf(chemin: Path) -> dict:
    with pdfplumber.open(chemin) as pdf:
        if not pdf.pages:
            raise ExtractionError("PDF vide (aucune page)")
        page = pdf.pages[0]
        texte = page.extract_text() or ""

        if texte.strip():
            # Tout ce qui dépend de `page` (extract_words) doit rester dans le `with` :
            # les ressources pdfminer sous-jacentes sont libérées à la sortie du contexte.
            mots = page.extract_words() or []
            return _construire_resultat(mots, page.height, texte)

        # Pas de couche texte -> PDF scanné (image encapsulée dans le PDF, sans
        # OCR fait en amont). On rasterise la page en image et on retombe sur le
        # même moteur OCR que pour un fichier image direct (2026-08-16, demande
        # Anis d'étendre la couverture image aux PDF scannés).
        try:
            page_rasterisee = page.to_image(resolution=200).original
        except Exception as e:
            raise ExtractionError(
                f"PDF scanné (aucune couche texte) et impossible à rasteriser pour l'OCR : {e}"
            )
        return _resultat_par_ocr(page_rasterisee)


_moteur_ocr = None


def _moteur_ocr_partage():
    """Charge RapidOCR une seule fois (les modèles ONNX restent en mémoire d'un
    appel à l'autre) — le premier appel est plus lent le temps du chargement."""
    global _moteur_ocr
    if _moteur_ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        _moteur_ocr = RapidOCR()
    return _moteur_ocr


def _resultat_par_ocr(source: Path | Image.Image) -> dict:
    """Fait tourner l'OCR sur `source` (chemin de fichier image, ou image PIL déjà
    en mémoire — cas d'une page de PDF scanné rasterisée) et construit le résultat
    via les mêmes heuristiques que le chemin PDF texte."""
    entree = np.asarray(source) if isinstance(source, Image.Image) else str(source)
    resultat, _ = _moteur_ocr_partage()(entree)
    if not resultat:
        raise ExtractionError(
            "Aucun texte reconnu sur cette image (photo/scan illisible ou vide) — "
            "champs à compléter manuellement."
        )

    mots = []
    for boite, texte_bloc, _score in resultat:
        xs = [pt[0] for pt in boite]
        ys = [pt[1] for pt in boite]
        mots.append({"text": texte_bloc, "x0": min(xs), "x1": max(xs), "top": min(ys)})

    if isinstance(source, Image.Image):
        hauteur = float(source.height)
    else:
        with Image.open(source) as img:
            hauteur = float(img.height)

    lignes_pos = regrouper_lignes_par_position(mots)
    texte = "\n".join(" ".join(w["text"] for w in ligne) for ligne in lignes_pos)
    return _construire_resultat(mots, hauteur, texte)


def _lire_image(chemin: Path) -> dict:
    return _resultat_par_ocr(chemin)


PROMPT_IA = """Tu es un assistant d'extraction de données comptables. Analyse l'image de \
cette facture (achat ou vente) et réponds UNIQUEMENT avec un objet JSON strict, sans \
texte autour, ni bloc de code, avec exactement ces champs :

{
  "date": "YYYY-MM-DD ou null",
  "numero": "numéro de facture (chaîne) ou null",
  "montant_ht": nombre ou null,
  "montant_tva": nombre ou null,
  "montant_ttc": nombre ou null,
  "sujet_detecte": "objet/désignation de la facture ou null",
  "nom_tiers_detecte": "nom de la société émettrice de la facture ou null"
}

Règles : les montants sont des nombres (point décimal, sans symbole monétaire ni espace). \
Si une information est absente ou illisible, mets null — n'invente jamais une valeur."""


def _page_en_image_png(chemin: Path) -> bytes:
    """Convertit la 1ère page/l'image du fichier en PNG (bytes), pour l'envoyer à un
    modèle de vision. Réutilise la même rasterisation que le repli OCR local pour un PDF."""
    if chemin.suffix.lower() == ".pdf":
        with pdfplumber.open(chemin) as pdf:
            if not pdf.pages:
                raise ExtractionError("PDF vide (aucune page)")
            image = pdf.pages[0].to_image(resolution=200).original
    else:
        image = Image.open(chemin)
        image.load()

    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def _extraire_json_reponse(texte: str) -> dict:
    """Le modèle peut entourer le JSON de texte ou d'un bloc ```json``` malgré la
    consigne — on isole la première accolade ouvrante à la dernière fermante."""
    debut, fin = texte.find("{"), texte.rfind("}")
    if debut == -1 or fin == -1 or fin < debut:
        raise ExtractionError("Réponse IA sans JSON exploitable")
    return json.loads(texte[debut:fin + 1])


def _valeur_ou_none(d: dict, cle: str):
    v = d.get(cle)
    return v if v not in ("", "null", None) else None


def extraire_facture_ia(chemin: Path) -> dict:
    """Extraction via un modèle de vision distant (OpenRouter). Lève ExtractionError
    sur tout échec (réseau, clé absente/invalide, réponse inexploitable) — c'est
    l'appelant qui décide de retomber sur le moteur local dans ce cas."""
    if not OPENROUTER_API_KEY:
        raise ExtractionError("OPENROUTER_API_KEY non configurée")

    import httpx

    png = _page_en_image_png(chemin)
    data_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")

    try:
        reponse = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "X-Title": "Kikou - Numerisation factures",
            },
            json={
                "model": OPENROUTER_MODEL,
                "temperature": 0,
                "messages": [
                    {"role": "user", "content": [
                        {"type": "text", "text": PROMPT_IA},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ]}
                ],
            },
            timeout=OPENROUTER_TIMEOUT,
        )
        reponse.raise_for_status()
        contenu = reponse.json()["choices"][0]["message"]["content"]
        brut = _extraire_json_reponse(contenu)
    except ExtractionError:
        raise
    except Exception as e:
        raise ExtractionError(f"Appel OpenRouter échoué : {e}")

    date_extraite = None
    date_texte = _valeur_ou_none(brut, "date")
    if date_texte:
        try:
            date_extraite = date.fromisoformat(str(date_texte)[:10])
        except ValueError:
            date_extraite = None

    return {
        "date": date_extraite,
        "numero": _valeur_ou_none(brut, "numero"),
        "montant_ht": _normaliser_nombre(str(brut["montant_ht"])) if _valeur_ou_none(brut, "montant_ht") is not None else None,
        "montant_tva": _normaliser_nombre(str(brut["montant_tva"])) if _valeur_ou_none(brut, "montant_tva") is not None else None,
        "montant_ttc": _normaliser_nombre(str(brut["montant_ttc"])) if _valeur_ou_none(brut, "montant_ttc") is not None else None,
        "sujet_detecte": _valeur_ou_none(brut, "sujet_detecte"),
        "nom_tiers_detecte": _valeur_ou_none(brut, "nom_tiers_detecte"),
    }


def extraire_facture(chemin: Path) -> dict:
    """
    Lit une facture PDF ou image et repère les champs. Si OPENROUTER_API_KEY est
    configurée, tente d'abord l'extraction par IA de vision (extraire_facture_ia) ;
    en cas d'échec (ou si aucune clé n'est configurée), retombe sur le moteur local
    (pdfplumber pour le texte natif, OCR RapidOCR en repli pour les scans/images).
    Lève ExtractionError seulement si aucun des deux moteurs n'a rien pu extraire —
    les champs restent alors à compléter manuellement, jamais de valeur devinée.
    """
    suffixe = chemin.suffix.lower()
    if suffixe != ".pdf" and suffixe not in EXTENSIONS_IMAGE:
        raise ExtractionError(
            "Lecture automatique disponible uniquement pour les PDF avec texte "
            "ou les images (JPG, PNG…) — champs à compléter manuellement."
        )

    if OPENROUTER_API_KEY:
        try:
            return extraire_facture_ia(chemin)
        except ExtractionError:
            pass  # repli silencieux sur le moteur local ci-dessous

    try:
        if suffixe == ".pdf":
            return _lire_pdf(chemin)
        else:
            return _lire_image(chemin)
    except ExtractionError:
        raise
    except Exception as e:
        raise ExtractionError(f"Impossible de lire le fichier : {e}")
