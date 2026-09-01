# parsers/saham.py
# Parser pour les relevés Saham Bank (PDF natif) — calé sur un relevé réel (juillet 2026).
# Saham Bank est le nom actuel de l'ex-Société Générale Marocaine de Banques (SGMA,
# contact.Sgmaroc@socgen.com apparaît encore dans l'en-tête du relevé) — à ne pas
# confondre avec la Société Générale française (parsers/societe_generale.py).
#
# pdfplumber.extract_tables() détecte bien le tableau bordé, mais faute de séparateurs
# horizontaux entre les lignes de mouvement, toutes les valeurs d'une même colonne sont
# fusionnées dans UNE cellule, une ligne par mouvement séparée par "\n" (ex. la colonne
# Libellé contient tous les libellés du relevé, un par ligne) : on retrouve chaque
# transaction en découpant chaque colonne sur "\n" et en recombinant par index de ligne.
# Date opération et date valeur sont au format JJ/MM (sans année) — l'année est reprise de
# l'en-tête "Relevé du JJ/MM/AA au JJ/MM/AA".

import re
from datetime import date
from pathlib import Path
from typing import Optional

import pdfplumber

from .base import BaseParser, Transaction

RE_DATE = re.compile(r'^(\d{2})/(\d{2})$')
RE_PERIODE = re.compile(r'Relev[ée]\s+du\s+\d{2}/\d{2}/(\d{2})\s+au\s+\d{2}/\d{2}/(\d{2})')


def _normaliser_montant(texte: str) -> Optional[float]:
    """Convertit '1.100,00', '20.383,06', '93,50'... en float (dernier séparateur
    rencontré = décimale, l'autre = groupement de milliers, retiré)."""
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


class SahamBankParser(BaseParser):
    NOM_BANQUE = "Saham Bank"

    def can_parse(self, texte_complet: str) -> bool:
        texte = texte_complet.upper()
        if "SAHAM BANK" in texte or "SAHAMBANK" in texte:
            return True
        # Ex-Société Générale Marocaine de Banques, encore identifiable par son domaine mail.
        return "SGMAROC" in texte.replace(" ", "").replace("-", "")

    def _annee_debut(self, pdf) -> int:
        for page in pdf.pages[:1]:
            texte = page.extract_text() or ""
            m = RE_PERIODE.search(texte)
            if m:
                return 2000 + int(m.group(1))
        return date.today().year

    def parse(self, chemin_pdf: str) -> list[Transaction]:
        nom_fichier = Path(chemin_pdf).name
        transactions: list[Transaction] = []

        with pdfplumber.open(chemin_pdf) as pdf:
            annee = self._annee_debut(pdf)
            for page in pdf.pages:
                for table in page.extract_tables():
                    transactions.extend(self._parser_table(table, annee, nom_fichier))

        return transactions

    def _parser_table(self, table: list[list], annee: int, nom_fichier: str) -> list[Transaction]:
        transactions: list[Transaction] = []
        for row in table:
            cols = [c if c is not None else "" for c in row]
            if len(cols) < 5:
                continue
            lignes_date_op = cols[0].split("\n")
            if not any(RE_DATE.match(l.strip()) for l in lignes_date_op):
                continue
            lignes_libelle = cols[2].split("\n")
            lignes_debit = cols[3].split("\n")
            lignes_credit = cols[4].split("\n")

            for i, date_op_txt in enumerate(lignes_date_op):
                m = RE_DATE.match(date_op_txt.strip())
                if not m:
                    continue
                jour, mois = m.groups()
                try:
                    date_op = date(annee, int(mois), int(jour))
                except ValueError:
                    continue
                libelle = lignes_libelle[i].strip() if i < len(lignes_libelle) else ""
                debit = _normaliser_montant(lignes_debit[i]) if i < len(lignes_debit) else None
                credit = _normaliser_montant(lignes_credit[i]) if i < len(lignes_credit) else None
                if debit is None and credit is None:
                    continue
                transactions.append(Transaction(
                    date=date_op, libelle=libelle, debit=debit, credit=credit,
                    solde=None, banque=self.NOM_BANQUE, fichier_source=nom_fichier,
                ))
        return transactions
