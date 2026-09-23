# parsers/credit_mutuel.py
# Parser pour les relevés Crédit Mutuel (PDF natif) — calé sur un relevé réel RDUTY
# (Caisse 06162, C/C EUROCOMPTE PRO, févr. 2026). pdfplumber détecte ici un vrai tableau
# bordé (contrairement à Société Générale/BRED) : on lit directement via extract_tables(),
# colonnes repérées par l'en-tête "Date | Date valeur | Opération | Débit EUROS | Crédit
# EUROS" plutôt que par position — plus fiable que l'heuristique par signe du parser
# générique (les montants ne sont jamais signés dans le texte). Les lignes de détail
# supplémentaires (référence de virement, nom du bénéficiaire sur plusieurs lignes...) sont
# des lignes de tableau à part sans date : rattachées au libellé de la transaction précédente.

import re
from datetime import date
from pathlib import Path
from typing import Optional

import pdfplumber

from .base import BaseParser, Transaction

RE_DATE = re.compile(r'^(\d{2})/(\d{2})/(\d{4})$')


def _normaliser_montant(texte: str) -> Optional[float]:
    """Convertit '10.800,00', '1 234,56', '35,57'... en float (dernier séparateur
    rencontré = décimale, l'autre = groupement de milliers, retiré)."""
    t = (texte or "").strip().replace(" ", "").replace(" ", "")
    t = re.sub(r"[^\d,.\-]", "", t)
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


class CreditMutuelParser(BaseParser):
    NOM_BANQUE = "Crédit Mutuel"

    def can_parse(self, texte_complet: str) -> bool:
        texte = texte_complet.upper()
        return "CREDIT MUTUEL" in texte or "CRÉDIT MUTUEL" in texte

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

        entete = None
        for row in table:
            cols = [(c or "").strip() for c in row]
            if cols and cols[0] == "Date" and any("bit" in c.lower() for c in cols):
                entete = cols
                break
        if entete is None:
            return transactions  # pas le tableau des mouvements (ex. bloc d'en-tête de page)

        idx_debit = next((i for i, c in enumerate(entete) if "bit" in c.lower()), None)
        idx_credit = next((i for i, c in enumerate(entete) if "dit" in c.lower() and i != idx_debit), None)
        if idx_debit is None or idx_credit is None:
            return transactions

        transaction_courante: Optional[Transaction] = None

        for row in table:
            cols = [(c or "").strip() for c in row]
            if not any(cols) or cols == entete:
                continue

            texte_ligne = " ".join(cols).upper()
            if "SOLDE" in texte_ligne or "TOTAL DES MOUVEMENTS" in texte_ligne:
                # Ligne de solde initial/final ou de total — termine la transaction en
                # cours, n'en fait pas partie.
                if transaction_courante:
                    transactions.append(transaction_courante)
                    transaction_courante = None
                continue

            m = RE_DATE.match(cols[0]) if cols[0] else None

            if m:
                if transaction_courante:
                    transactions.append(transaction_courante)
                j, mo, a = m.groups()
                try:
                    date_op = date(int(a), int(mo), int(j))
                except ValueError:
                    transaction_courante = None
                    continue
                libelle = cols[2] if len(cols) > 2 else ""
                debit = _normaliser_montant(cols[idx_debit]) if idx_debit < len(cols) else None
                credit = _normaliser_montant(cols[idx_credit]) if idx_credit < len(cols) else None
                transaction_courante = Transaction(
                    date=date_op, libelle=libelle, debit=debit, credit=credit,
                    solde=None, banque=self.NOM_BANQUE, fichier_source=nom_fichier,
                )
            elif transaction_courante is not None:
                # Ligne de détail sans date (référence de virement, nom du bénéficiaire...)
                detail = cols[2] if len(cols) > 2 and cols[2] else next((c for c in cols if c), "")
                if detail and detail not in transaction_courante.libelle:
                    transaction_courante.libelle = f"{transaction_courante.libelle} {detail}".strip()

        if transaction_courante:
            transactions.append(transaction_courante)

        return transactions
