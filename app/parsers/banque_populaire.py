# parsers/banque_populaire.py
# Parser pour les relevés Banque Populaire (Maroc) — calé sur un relevé réel (5 pages,
# 2025), reçu comme un scan de mauvaise qualité : la couche texte native du PDF est
# corrompue (caractères inversés/mélangés, illisible), donc ce parseur passe toujours par
# l'OCR (voir ocr_utils.py) plutôt que par le texte natif.
#
# Contrairement à Crédit du Maroc, l'OCR découpe la zone "Date opération / Date valeur /
# Référence" de façon très inconsistante d'une ligne à l'autre — parfois un seul bloc de 16
# chiffres collés (les 2 dates JJMMAAAA sans séparateur), parfois 2 blocs de 8, parfois la
# date opération elle-même éclatée en 3 blocs ("17"/"07"/"2025"), parfois la référence
# (chiffres ou alphanumérique) recollée juste après les dates dans le même bloc. Plutôt que
# reconnaître un format de date précis, on concatène tous les blocs de cette zone (repérés
# par leur position, avant le libellé) et on ne garde que les CHIFFRES qui s'y trouvent,
# dans l'ordre — les 8 premiers donnent la date opération (JJMMAAAA), les 8 suivants la
# date valeur ; le reste (référence) est ignoré, pas nécessaire à la comptabilité.

import re
from datetime import date
from pathlib import Path
from typing import Optional

import pdfplumber

from .base import BaseParser, Transaction, regrouper_lignes_par_position
from .ocr_utils import page_sans_texte, ocr_mots_page, seuil_debit_credit

RE_MONTANT_FIN = re.compile(r'^\d+,\d{2}$')
RE_GROUPE_MILLIERS = re.compile(r'^\d{1,3}$')

X0_FIN_ZONE_DATE = 500  # au-delà, c'est le libellé (calibré sur le relevé réel)


def _normaliser_montant(texte: str) -> Optional[float]:
    t = (texte or "").replace(" ", "").replace(",", ".")
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        return None


class BanquePopulaireMarocParser(BaseParser):
    NOM_BANQUE = "Banque Populaire"

    def can_parse(self, texte_complet: str) -> bool:
        texte = texte_complet.upper()
        if "BANQUE CENTRALE POPULAIRE" in texte or "GROUPE BCP" in texte:
            return True
        # "BANQUE POPULAIRE" seul est ambigu avec le réseau français (BRED en fait
        # partie) — exigé avec "MAROC", sauf si le texte est illisible/corrompu (relevé
        # scanné, cas visé par ce parseur) : dans ce cas se rabattre sur le nom de banque
        # seul est le seul repère disponible.
        if "BANQUE POPULAIRE" in texte and "MAROC" in texte:
            return True
        return "BANQUEPOPULAIRE" in texte.replace(" ", "")

    def _extraire_ligne(self, ligne: list[dict]) -> Optional[dict]:
        zone_date = [w for w in ligne if w["x0"] < X0_FIN_ZONE_DATE]
        reste = [w for w in ligne if w["x0"] >= X0_FIN_ZONE_DATE]
        if len(zone_date) < 1 or len(reste) < 1:
            return None

        chiffres = "".join(c for w in zone_date for c in w["text"] if c.isdigit())
        if len(chiffres) < 16:
            return None
        jour_op, mois_op, annee_op = chiffres[0:2], chiffres[2:4], chiffres[4:8]
        jour_val, mois_val, annee_val = chiffres[8:10], chiffres[10:12], chiffres[12:16]

        if not RE_MONTANT_FIN.match(reste[-1]["text"]):
            return None
        idx = len(reste) - 1
        montant_mots = [reste[idx]]
        while (idx - 1 >= 0 and RE_GROUPE_MILLIERS.match(reste[idx - 1]["text"])
               and (reste[idx]["x0"] - reste[idx - 1]["x0"]) <= 25):
            idx -= 1
            montant_mots.insert(0, reste[idx])

        libelle = " ".join(w["text"] for w in reste[:idx])
        montant = _normaliser_montant("".join(w["text"] for w in montant_mots))
        if montant is None:
            return None

        return {
            "jour": jour_op, "mois": mois_op, "annee": annee_op,
            "jour_val": jour_val, "mois_val": mois_val, "annee_val": annee_val,
            "libelle": libelle, "montant": montant, "x1_montant": montant_mots[-1]["x1"],
        }

    def _lignes_page(self, page) -> list[dict]:
        """Essaie d'abord le texte natif ; si ça ne donne aucune ligne exploitable (page
        sans texte, ou — vu sur un relevé réel — texte natif présent mais corrompu par un
        encodage de police défaillant), retente en OCR."""
        if not page_sans_texte(page):
            mots = page.extract_words(x_tolerance=1)
            lignes = [r for l in regrouper_lignes_par_position(mots, seuil=2.0)
                      if (r := self._extraire_ligne(l))]
            if lignes:
                return lignes

        mots = ocr_mots_page(page)
        # Jusqu'à ~9pt de gigue verticale observée sur ce relevé entre le début et la fin
        # d'une même ligne de tableau (plus que Crédit du Maroc) — seuil un peu plus large.
        return [r for l in regrouper_lignes_par_position(mots, seuil=12.0)
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
                date_op = date(int(r["annee"]), int(r["mois"]), int(r["jour"]))
            except ValueError:
                continue
            # Pas d'écart significatif détecté (relevé/page ne comportant qu'une seule
            # colonne mouvementée) -> tout classer en débit, le cas le plus courant.
            est_credit = seuil is not None and r["x1_montant"] > seuil
            transactions.append(Transaction(
                date=date_op, libelle=r["libelle"],
                debit=None if est_credit else r["montant"],
                credit=r["montant"] if est_credit else None,
                solde=None, banque=self.NOM_BANQUE, fichier_source=nom_fichier,
            ))
        return transactions
