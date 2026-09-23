# parsers/saham.py
# Parser pour les relevés Saham Bank / Société Générale Maroc (PDF natif).
# Format par ligne : JJ/MM JJ/MM <libellé> <montant>
# En-têtes "Débit"/"Crédit" explicitement présents dans le texte : on les
# utilise en priorité pour situer les deux colonnes.

import re
from datetime import date
from pathlib import Path
from typing import Optional

import pdfplumber

from .base import BaseParser, Transaction
from .util_position import grouper_lignes, extraire_montant_final, seuil_colonnes, est_credit

PATTERN_DATE = re.compile(r'^(\d{2})/(\d{2})$')
PATTERN_ANNEE = re.compile(r'\d{2}/\d{2}/(\d{2,4})')

MOTS_CLES_CREDIT = ('VIREMENT RECU', 'VERSEMENT', 'REMISE CHEQUES', 'ENCAISSEMENT')


class SahamParser(BaseParser):
    NOM_BANQUE = "Saham Bank"

    def can_parse(self, texte_complet: str) -> bool:
        return "Saham Bank" in texte_complet or "sahambank.com" in texte_complet or "socgen.com" in texte_complet

    def _extraire_annee(self, texte: str) -> int:
        m = PATTERN_ANNEE.search(texte)
        if not m:
            return date.today().year
        a = int(m.group(1))
        return 2000 + a if a < 100 else a

    def _seuil_par_entetes(self, page) -> Optional[float]:
        """Cherche les mots 'Débit'/'Crédit' pour situer précisément les colonnes."""
        for mot in page.extract_words():
            texte = mot['text'].lower()
            if 'cr' in texte and 'dit' in texte and len(texte) < 10:
                # Position du mot "Crédit" : le seuil est juste avant son début
                return mot['x0'] - 20
        return None

    def parse(self, chemin_pdf: str) -> list[Transaction]:
        nom_fichier = Path(chemin_pdf).name
        transactions = []

        with pdfplumber.open(chemin_pdf) as pdf:
            texte_complet = "\n".join(p.extract_text() or "" for p in pdf.pages)
            annee = self._extraire_annee(texte_complet)

            for page in pdf.pages:
                lignes = grouper_lignes(page)
                seuil = self._seuil_par_entetes(page) or seuil_colonnes(lignes)

                for top in sorted(lignes):
                    mots = lignes[top]
                    if len(mots) < 3:
                        continue
                    if not (PATTERN_DATE.match(mots[0]['text']) and PATTERN_DATE.match(mots[1]['text'])):
                        continue

                    r = extraire_montant_final(mots)
                    if not r:
                        continue
                    valeur, x1, idx_montant = r

                    libelle = ' '.join(w['text'] for w in mots[2:idx_montant]).strip()
                    if not libelle:
                        continue

                    jour, mois = mots[1]['text'].split('/')  # date valeur
                    try:
                        tx_date = date(annee, int(mois), int(jour))
                    except ValueError:
                        continue

                    credit = est_credit(x1, seuil)
                    if credit is None:
                        credit = any(mc in libelle.upper() for mc in MOTS_CLES_CREDIT)

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
