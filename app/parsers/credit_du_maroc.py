# parsers/credit_du_maroc.py
# Parser pour les relevés Crédit du Maroc — calé sur un relevé réel (juillet 2026), reçu
# entièrement scanné (aucune couche texte native, juste une image par page) : passe par
# l'OCR (voir ocr_utils.py) plutôt que par pdfplumber.extract_text()/extract_tables().
#
# Tableau à colonnes bien séparées : Date Opération | Valeur | Libellé | Débit | Crédit,
# chaque date étant reconnue par l'OCR comme un seul bloc "JJ MM AA" (année sur 2
# chiffres). Débit et Crédit sont distingués par la position horizontale du bloc montant
# (bord droit, colonnes alignées à droite) — seuil calculé une fois pour tout le document,
# au plus grand écart entre deux positions observées (comme pour CIH Bank), plutôt qu'une
# position fixe en pixels (dépendante de la résolution de rastérisation).

import re
from datetime import date
from pathlib import Path
from typing import Optional

import pdfplumber

from .base import BaseParser, Transaction, regrouper_lignes_par_position
from .ocr_utils import page_sans_texte, ocr_mots_page, seuil_debit_credit

RE_MONTANT = re.compile(r'^\d[\d ]*[.,]\d{2}$')


def _date_ocr(texte: str) -> Optional[tuple[str, str, str]]:
    """Reconnaît une date JJ MM AA reconnue par l'OCR comme un seul bloc — l'espacement
    entre les 3 groupes de 2 chiffres est parfois perdu par l'OCR (ex. '3006 26' au lieu
    de '30 06 26'), donc on retire tous les espaces puis on retranche par blocs de 2."""
    chiffres = (texte or "").replace(" ", "")
    if re.fullmatch(r'\d{6}', chiffres):
        return chiffres[0:2], chiffres[2:4], chiffres[4:6]
    return None


def _normaliser_montant(texte: str) -> Optional[float]:
    t = (texte or "").strip().replace(" ", "").replace(" ", "")
    if not t:
        return None
    if "," in t and "." in t:
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


class CreditDuMarocParser(BaseParser):
    NOM_BANQUE = "Crédit du Maroc"

    def can_parse(self, texte_complet: str) -> bool:
        texte = texte_complet.upper().replace(" ", "")
        return "CREDITDUMAROC" in texte or "CREDIROC" in texte

    def _extraire_ligne(self, ligne: list[dict]) -> Optional[dict]:
        if len(ligne) < 4:
            return None
        date_op = _date_ocr(ligne[0]["text"])
        if date_op is None:
            return None
        if _date_ocr(ligne[1]["text"]) is None:
            return None
        if not RE_MONTANT.match(ligne[-1]["text"].strip()):
            return None

        libelle = " ".join(w["text"] for w in ligne[2:-1])
        montant = _normaliser_montant(ligne[-1]["text"])
        if montant is None:
            return None

        jour, mois, annee_2c = date_op
        return {
            "jour": jour, "mois": mois, "annee": 2000 + int(annee_2c),
            "libelle": libelle, "montant": montant, "x1_montant": ligne[-1]["x1"],
        }

    def _lignes_page(self, page) -> list[dict]:
        """Essaie d'abord le texte natif ; si ça ne donne aucune ligne exploitable (page
        sans texte, ou texte natif présent mais corrompu par un encodage de police
        défaillant), retente en OCR. L'OCR positionne les mots avec un peu plus de gigue
        verticale que pdfplumber (jusqu'à quelques pixels) — seuil de regroupement par
        ligne plus large pour ne pas couper une même ligne de tableau en deux."""
        if not page_sans_texte(page):
            mots = page.extract_words(x_tolerance=1)
            lignes = [r for l in regrouper_lignes_par_position(mots, seuil=2.0)
                      if (r := self._extraire_ligne(l))]
            if lignes:
                return lignes

        mots = ocr_mots_page(page)
        return [r for l in regrouper_lignes_par_position(mots, seuil=6.0)
                if (r := self._extraire_ligne(l))]

    def parse(self, chemin_pdf: str) -> list[Transaction]:
        nom_fichier = Path(chemin_pdf).name
        lignes_brutes: list[dict] = []

        with pdfplumber.open(chemin_pdf) as pdf:
            for page in pdf.pages:
                lignes_brutes.extend(self._lignes_page(page))

        if not lignes_brutes:
            return []

        seuil = seuil_debit_credit([r["x1_montant"] for r in lignes_brutes])

        transactions: list[Transaction] = []
        for r in lignes_brutes:
            try:
                date_op = date(r["annee"], int(r["mois"]), int(r["jour"]))
            except ValueError:
                continue
            # Pas d'écart significatif détecté (relevé ne comportant qu'une seule colonne
            # mouvementée) -> tout classer en débit, le cas le plus courant.
            est_credit = seuil is not None and r["x1_montant"] > seuil
            transactions.append(Transaction(
                date=date_op, libelle=r["libelle"],
                debit=None if est_credit else r["montant"],
                credit=r["montant"] if est_credit else None,
                solde=None, banque=self.NOM_BANQUE, fichier_source=nom_fichier,
            ))
        return transactions
