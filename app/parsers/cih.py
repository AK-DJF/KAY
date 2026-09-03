# parsers/cih.py
# Parser pour les relevés CIH Bank (PDF natif) — calé sur un relevé réel (mai 2026,
# compte NEOARTS, 2 pages).
#
# Comme BMCE, le tableau n'a pas de bordures détectables par extract_tables() : on
# travaille sur les mots positionnés (extract_words). Particularité de ce gabarit : la
# date opération et la date valeur (chacune JJ/MM, sans année) sont un seul mot glué sans
# espace ("06/0506/05" pour operation=06/05 et valeur=06/05), pas deux mots séparés.
#
# Un montant peut être coupé par pdfplumber en plusieurs mots aux séparateurs de milliers
# (ex. "600 000,00" -> "600"/"000,00") : reconstruit en remontant depuis la fin de la
# ligne (dernier mot décimal, puis groupes de chiffres tant que l'écart horizontal reste
# petit — le relevé CIH a des écarts un peu plus larges que BMCE, jusqu'à ~25pt, d'où un
# seuil à 30pt ici).
#
# Débit ou crédit : à la différence de BMCE (où la date valeur juste avant le montant sert
# de repère), rien ne précède directement le montant ici (le libellé est de longueur
# variable) — on classe donc par un seuil de position horizontale déterminé une fois pour
# tout le document, au plus grand écart entre deux positions de montant observées (les
# montants Débit et Crédit occupent deux bandes bien séparées sur ce gabarit) ; calculé sur
# tout le document plutôt que par page pour rester fiable même sur une page qui ne contient
# que des débits ou que des crédits.
# L'année (absente des dates) est reprise de la ligne "SOLDE DEPART AU : JJ/MM/AAAA".

import re
from datetime import date
from pathlib import Path
from typing import Optional

import pdfplumber

from .base import BaseParser, Transaction, regrouper_lignes_par_position
from .ocr_utils import seuil_debit_credit

RE_DATE_COLLEE = re.compile(r'^(\d{2}/\d{2})(\d{2}/\d{2})$')
RE_MONTANT_FIN = re.compile(r'^\d+,\d{2}$')
RE_GROUPE_MILLIERS = re.compile(r'^\d{1,3}$')
RE_SOLDE_DEPART = re.compile(r'SOLDE DEPART AU\s*:?\s*\d{2}/\d{2}/(\d{4})')


def _normaliser_montant(texte: str) -> Optional[float]:
    t = (texte or "").replace(" ", "").replace(",", ".")
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        return None


class CIHBankParser(BaseParser):
    NOM_BANQUE = "CIH Bank"

    def can_parse(self, texte_complet: str) -> bool:
        # Insensible aux espaces insérés par l'extraction de texte (variable selon la
        # version de la bibliothèque sous-jacente) — "C.I.H" ou "CIH BANK" peuvent être
        # rendus avec des espacements différents suivant l'environnement (ex. "C. I. H").
        sans_espaces = re.sub(r'\s+', '', texte_complet.upper())
        return (
            "CIHBANK" in sans_espaces
            or "CIH.CO.MA" in sans_espaces
            or ("C.I.H" in sans_espaces and "MAROC" in sans_espaces)
        )

    def _annee(self, pdf) -> int:
        for page in pdf.pages[:1]:
            texte = page.extract_text() or ""
            m = RE_SOLDE_DEPART.search(texte)
            if m:
                return int(m.group(1))
        return date.today().year

    def _extraire_ligne(self, ligne: list[dict]) -> Optional[dict]:
        if len(ligne) < 3:
            return None
        m = RE_DATE_COLLEE.match(ligne[0]["text"])
        if not m:
            return None
        if not RE_MONTANT_FIN.match(ligne[-1]["text"]):
            return None

        idx = len(ligne) - 1
        montant_mots = [ligne[idx]]
        while (idx - 1 >= 1 and RE_GROUPE_MILLIERS.match(ligne[idx - 1]["text"])
               and (ligne[idx]["x0"] - ligne[idx - 1]["x0"]) <= 30):
            idx -= 1
            montant_mots.insert(0, ligne[idx])

        libelle = " ".join(w["text"] for w in ligne[1:idx])
        montant = _normaliser_montant("".join(w["text"] for w in montant_mots))
        if montant is None:
            return None

        return {
            "jour_op": m.group(1).split("/")[0], "mois_op": m.group(1).split("/")[1],
            "libelle": libelle, "montant": montant, "x0_montant": montant_mots[0]["x0"],
        }

    def parse(self, chemin_pdf: str) -> list[Transaction]:
        nom_fichier = Path(chemin_pdf).name
        lignes_brutes: list[dict] = []

        with pdfplumber.open(chemin_pdf) as pdf:
            annee = self._annee(pdf)
            for page in pdf.pages:
                words = page.extract_words(x_tolerance=1)
                for ligne in regrouper_lignes_par_position(words):
                    r = self._extraire_ligne(ligne)
                    if r:
                        lignes_brutes.append(r)

        if not lignes_brutes:
            return []

        seuil = seuil_debit_credit([r["x0_montant"] for r in lignes_brutes])

        transactions: list[Transaction] = []
        for r in lignes_brutes:
            try:
                date_op = date(annee, int(r["mois_op"]), int(r["jour_op"]))
            except ValueError:
                continue
            # Pas d'écart significatif détecté (relevé/page ne comportant qu'une seule
            # colonne mouvementée) -> tout classer en débit, le cas le plus courant.
            est_credit = seuil is not None and r["x0_montant"] > seuil
            transactions.append(Transaction(
                date=date_op, libelle=r["libelle"],
                debit=None if est_credit else r["montant"],
                credit=r["montant"] if est_credit else None,
                solde=None, banque=self.NOM_BANQUE, fichier_source=nom_fichier,
            ))
        return transactions
