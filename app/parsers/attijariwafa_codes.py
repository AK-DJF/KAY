# parsers/attijariwafa_codes.py
# Parseur pour les relevés Attijariwafa bank au format "code opération" — différent du
# format MarocColonnesParser (celui-ci a des lignes "DD MM {libellé} DD MM {montant}" avec
# deux dates propres). Ici chaque ligne est : {code}{JJ} {MM} {libellé...} {JJ} {MM} {AAAA}
# {montant} — le jour de l'opération est collé en suffixe du code (ex. "0016BK03" -> jour
# "03"), suivi du mois seul ; puis vient une "date valeur" complète (JJ MM AAAA) juste avant
# le montant, qui sert ici de source fiable pour l'année. Les lignes sont triées par date
# d'opération (code), pas par date valeur — c'est donc la date d'opération qui est utilisée
# comme date de la transaction, l'année étant reprise de la date valeur de la même ligne.
# Colonnes Débit/Crédit séparées par position (repérées empiriquement, cf. tests) plutôt que
# par des en-têtes de tableau (absentes de ce format). Certaines lignes ont leurs caractères
# émis un par un par le moteur PDF (espacement nul entre lettres d'un même mot, espacement
# large entre mots) — la reconstruction du texte se fait donc par seuil d'écart horizontal
# plutôt qu'en supposant un mot = un objet extract_words().

import re
from datetime import date
from pathlib import Path
from typing import Optional

import pdfplumber

from .base import BaseParser, Transaction, regrouper_lignes_par_position

RE_CODE_MOIS = re.compile(r'^(\S+)\s+(\d{2})$')
RE_ANNEE = re.compile(r'\b(20\d{2})\b')
RE_CODE_MOIS_DETECTION = re.compile(r'\b\d{2,6}[A-Z]{1,3}\d{0,3}\s+\d{2}\b')

X_FIN_ZONE_CODE = 90
X_FIN_ZONE_LIBELLE = 296
X_FIN_ZONE_DATE_VALEUR = 400
X_SEPARATION_DEBIT_CREDIT = 505

SEUIL_ESPACE = 4.0  # écart horizontal (pt) au-delà duquel on insère un espace réel


def _joindre_mots(mots: list[dict]) -> str:
    """Reconstruit le texte d'une zone en respectant les vrais espaces, même quand le PDF
    a émis chaque caractère comme un mot séparé (écart quasi nul entre lettres)."""
    mots_tries = sorted(mots, key=lambda w: w["x0"])
    morceaux: list[str] = []
    x1_precedent: Optional[float] = None
    for w in mots_tries:
        if x1_precedent is not None and (w["x0"] - x1_precedent) >= SEUIL_ESPACE:
            morceaux.append(" ")
        morceaux.append(w["text"])
        x1_precedent = w["x1"]
    return "".join(morceaux).strip()


def _normaliser_montant(texte: str) -> Optional[float]:
    t = re.sub(r"\s", "", texte or "")
    t = t.replace(",", ".")
    t = re.sub(r"[^\d.\-]", "", t)
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        return None


class AttijariwafaCodesParser(BaseParser):
    NOM_BANQUE = "Attijariwafa bank (Maroc)"

    def can_parse(self, texte_complet: str) -> bool:
        if "ATTIJARIWAFA" not in texte_complet.upper():
            return False
        return len(RE_CODE_MOIS_DETECTION.findall(texte_complet)) >= 2

    def parse(self, chemin_pdf: str) -> list[Transaction]:
        nom_fichier = Path(chemin_pdf).name
        transactions: list[Transaction] = []

        with pdfplumber.open(chemin_pdf) as pdf:
            for page in pdf.pages:
                mots = page.extract_words()
                if not mots:
                    continue

                for ligne in regrouper_lignes_par_position(mots):
                    mots_code = [w for w in ligne if w["x0"] < X_FIN_ZONE_CODE]
                    mots_libelle = [w for w in ligne if X_FIN_ZONE_CODE <= w["x0"] < X_FIN_ZONE_LIBELLE]
                    mots_date_valeur = [w for w in ligne if X_FIN_ZONE_LIBELLE <= w["x0"] < X_FIN_ZONE_DATE_VALEUR]
                    mots_montant = [w for w in ligne if w["x0"] >= X_FIN_ZONE_DATE_VALEUR]

                    if not mots_code or not mots_montant or not mots_date_valeur:
                        continue

                    texte_code = _joindre_mots(mots_code)
                    m_code = RE_CODE_MOIS.match(texte_code)
                    if not m_code:
                        continue
                    code, mois = m_code.groups()
                    jour = code[-2:]
                    if not (jour.isdigit() and mois.isdigit()):
                        continue

                    texte_date_valeur = _joindre_mots(mots_date_valeur)
                    m_annee = RE_ANNEE.search(texte_date_valeur)
                    if not m_annee:
                        continue
                    annee = int(m_annee.group(1))

                    try:
                        tx_date = date(annee, int(mois), int(jour))
                    except ValueError:
                        continue

                    libelle = _joindre_mots(mots_libelle)
                    if not libelle:
                        continue

                    valeur = _normaliser_montant(_joindre_mots(mots_montant))
                    if valeur is None:
                        continue

                    est_credit = mots_montant[0]["x0"] >= X_SEPARATION_DEBIT_CREDIT
                    transactions.append(Transaction(
                        date=tx_date,
                        libelle=libelle,
                        debit=None if est_credit else valeur,
                        credit=valeur if est_credit else None,
                        solde=None,
                        banque=self.NOM_BANQUE,
                        fichier_source=nom_fichier,
                    ))

        return transactions
