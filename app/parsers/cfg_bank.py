# parsers/cfg_bank.py
# Parser pour les relevés CFG Bank (PDF natif, généré par leur back-office
# "PROD.CFGMOROCCO" — identifiant visible dans le pied de page de chaque page).
# Format par ligne : JJ/MM/AAAA JJ/MM/AAAA {réf collée au libellé} {montant}, avec
# séparateur de milliers "," et décimale "." (ex: "25,000.00") — inverse de la
# convention "1 234,56" observée sur les autres banques marocaines gérées par cette
# appli. Pas de colonne Débit/Crédit séparée dans le texte linéaire : la colonne réelle
# est déduite par position, à partir des en-têtes "Débit"/"Crédit" de la page (mêmes
# outils que bmce.py/saham.py, cf. util_position.py, mais seuil calculé différemment
# ici car les en-têtes sont fiables sur chaque page).

import re
from datetime import date
from pathlib import Path
from typing import Optional

import pdfplumber

from .base import BaseParser, Transaction
from .util_position import grouper_lignes

RE_DATE = re.compile(r'^(\d{2})/(\d{2})/(\d{4})$')
RE_MONTANT = re.compile(r'^\d{1,3}(?:,\d{3})*\.\d{2}$')


def _montant_vers_nombre(texte: str) -> Optional[float]:
    try:
        return float(texte.replace(',', ''))
    except ValueError:
        return None


class CfgBankParser(BaseParser):
    NOM_BANQUE = "CFG Bank"

    def can_parse(self, texte_complet: str) -> bool:
        t = texte_complet.upper()
        return "CFGMOROCCO" in t or "CFG BANK" in t

    def _seuil_colonnes(self, page) -> Optional[float]:
        """Seuil x = milieu entre la fin de l'en-tête 'Débit' et le début de 'Crédit'."""
        debit_x1 = credit_x0 = None
        for w in page.extract_words():
            if w['text'] == 'Débit':
                debit_x1 = w['x1']
            elif w['text'] == 'Crédit':
                credit_x0 = w['x0']
        if debit_x1 is not None and credit_x0 is not None:
            return (debit_x1 + credit_x0) / 2
        return None

    def parse(self, chemin_pdf: str) -> list[Transaction]:
        nom_fichier = Path(chemin_pdf).name
        transactions: list[Transaction] = []

        with pdfplumber.open(chemin_pdf) as pdf:
            for page in pdf.pages:
                seuil = self._seuil_colonnes(page)
                if seuil is None:
                    continue

                for top in sorted((lignes := grouper_lignes(page))):
                    mots = lignes[top]
                    if len(mots) < 3:
                        continue
                    if not RE_DATE.match(mots[0]['text']):
                        continue

                    dernier = mots[-1]
                    if not RE_MONTANT.match(dernier['text']):
                        continue
                    valeur = _montant_vers_nombre(dernier['text'])
                    if valeur is None:
                        continue

                    texte_ligne = ' '.join(w['text'] for w in mots).upper()
                    if 'SOLDE' in texte_ligne or texte_ligne.startswith('TOTAL'):
                        continue

                    jour, mois, annee = mots[0]['text'].split('/')
                    try:
                        tx_date = date(int(annee), int(mois), int(jour))
                    except ValueError:
                        continue

                    idx_debut_libelle = 2 if RE_DATE.match(mots[1]['text']) else 1
                    libelle = ' '.join(w['text'] for w in mots[idx_debut_libelle:-1]).strip()
                    if not libelle:
                        continue

                    credit = dernier['x1'] > seuil
                    transactions.append(Transaction(
                        date=tx_date,
                        libelle=libelle,
                        debit=None if credit else valeur,
                        credit=valeur if credit else None,
                        solde=None,
                        banque=self.NOM_BANQUE,
                        fichier_source=nom_fichier,
                    ))

        return transactions
