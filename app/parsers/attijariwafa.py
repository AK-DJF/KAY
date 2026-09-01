# parsers/attijariwafa.py
# Parser pour les relevés Attijariwafa Bank (PDF natif) — calé sur 2 relevés réels
# (comptes JAMALI EXPRESS et L.M.B.A, mai/juillet 2026).
#
# Le PDF a un vrai tableau bordé, mais pdfplumber.extract_tables() fusionne la colonne
# CODE sur plusieurs lignes (pas de séparateurs horizontaux visibles dans cette colonne) —
# sans importance, ce code interne n'est pas utile à la comptabilité. Colonnes utiles,
# toujours aux mêmes index : [2]=Date opération (JJ MM, sans année), [3]=Libellé,
# [5]=Date valeur (JJ MM AAAA, avec année — reprise pour dater la transaction),
# [6]=Débit, [7]=Crédit.
#
# Deux subtilités trouvées sur les relevés réels :
# - Une transaction peut être coupée sur 2 lignes de tableau (date+libellé sur l'une,
#   montant+valeur sur l'autre, sans qu'aucune des deux ait tout) — état "en attente"
#   comme pour Crédit Mutuel.
# - La toute première ligne de mouvement de chaque page (hors la 1ère page) est parfois
#   rendue par pdfplumber avec un espace inséré entre CHAQUE caractère (ex. "0 8 0 7" au
#   lieu de "08 07") — un artefact de rendu de cette ligne précise, pas un vrai relevé
#   erroné. Detecté et corrigé pour les champs numériques (date/montant) ; le libellé de
#   cette ligne reste espacé (perte cosmétique, sans impact sur les montants).

import re
from datetime import date
from pathlib import Path
from typing import Optional

import pdfplumber

from .base import BaseParser, Transaction

RE_JJ_MM = re.compile(r'^\d{2}\s\d{2}$')
RE_JJ_MM_ESPACE = re.compile(r'^\d(\s\d){3}$')  # ex. "0 8 0 7" (corrompu)


def _sans_espaces(texte: str) -> str:
    return texte.replace(" ", "")


def _normaliser_montant(texte: str) -> Optional[float]:
    t = (texte or "").strip().replace(" ", "").replace(" ", "")
    if not t:
        return None
    t = t.replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


class AttijariwafaBankParser(BaseParser):
    NOM_BANQUE = "Attijariwafa Bank"

    def can_parse(self, texte_complet: str) -> bool:
        return "ATTIJARIWAFA" in texte_complet.upper()

    def _corriger_ligne_corrompue(self, date_txt, valeur_txt, debit_txt, credit_txt):
        """Répare l'artefact de rendu 'chaque caractère espacé' (1ère ligne de page) —
        ne touche que les champs numériques, jamais le libellé (irrécupérable proprement)."""
        if RE_JJ_MM_ESPACE.match(date_txt):
            date_despace = _sans_espaces(date_txt)
            date_txt = date_despace[:2] + " " + date_despace[2:]
            valeur_txt = _sans_espaces(valeur_txt)
            debit_txt = _sans_espaces(debit_txt)
            credit_txt = _sans_espaces(credit_txt)
        return date_txt, valeur_txt, debit_txt, credit_txt

    def parse(self, chemin_pdf: str) -> list[Transaction]:
        nom_fichier = Path(chemin_pdf).name
        transactions: list[Transaction] = []

        with pdfplumber.open(chemin_pdf) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables():
                    transactions.extend(self._parser_table(table, nom_fichier))

        return transactions

    def _parser_table(self, table: list[list], nom_fichier: str) -> list[Transaction]:
        transactions: list[Transaction] = []
        en_attente: Optional[tuple] = None  # (date_txt, libelle)

        for row in table:
            if len(row) < 8:
                continue
            cols = [c if c is not None else "" for c in row]
            date_txt = cols[2].strip()
            libelle_txt = cols[3].strip()
            valeur_txt = cols[5].strip()
            debit_txt = cols[6].strip()
            credit_txt = cols[7].strip()

            date_txt, valeur_txt, debit_txt, credit_txt = self._corriger_ligne_corrompue(
                date_txt, valeur_txt, debit_txt, credit_txt
            )
            a_montant = bool(debit_txt or credit_txt)

            if RE_JJ_MM.match(date_txt) and a_montant:
                tx = self._construire(date_txt, libelle_txt, valeur_txt, debit_txt, credit_txt, nom_fichier)
                if tx:
                    transactions.append(tx)
                en_attente = None
            elif RE_JJ_MM.match(date_txt) and libelle_txt and not a_montant:
                en_attente = (date_txt, libelle_txt)
            elif not date_txt and not libelle_txt and a_montant and en_attente:
                date_txt, libelle_txt = en_attente
                tx = self._construire(date_txt, libelle_txt, valeur_txt, debit_txt, credit_txt, nom_fichier)
                if tx:
                    transactions.append(tx)
                en_attente = None

        return transactions

    def _construire(self, date_txt, libelle, valeur_txt, debit_txt, credit_txt, nom_fichier) -> Optional[Transaction]:
        # Date d'opération réelle = colonne DATE (jour/mois) — la colonne VALEUR peut
        # tomber le mois précédent/suivant (ex. opération le 05/05, valeur au 30/04) ;
        # seule son année est reprise, la colonne DATE n'en porte pas.
        m_date = re.match(r'^(\d{2})\s(\d{2})$', date_txt)
        m_annee = re.search(r'(\d{4})$', valeur_txt)
        if not m_date or not m_annee:
            return None
        jour, mois = m_date.groups()
        annee = m_annee.group(1)
        try:
            date_op = date(int(annee), int(mois), int(jour))
        except ValueError:
            return None

        debit = _normaliser_montant(debit_txt)
        credit = _normaliser_montant(credit_txt)
        if debit is None and credit is None:
            return None

        return Transaction(
            date=date_op, libelle=libelle, debit=debit, credit=credit,
            solde=None, banque=self.NOM_BANQUE, fichier_source=nom_fichier,
        )
