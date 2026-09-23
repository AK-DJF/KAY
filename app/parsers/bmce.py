# parsers/bmce.py
# Parser pour les relevés BMCE / Bank Of Africa (PDF natif, export multi-folios).
# Format par ligne : JJ MM <libellé> JJ MM <montant>
# Pas de mention "Débit/Crédit" explicite dans le texte — la colonne
# (gauche = débit, droite = crédit) est déduite de la position x du montant.

import re
from datetime import date
from pathlib import Path
from typing import Optional

import pdfplumber

from .base import BaseParser, Transaction
from .util_position import grouper_lignes, extraire_montant_final, seuil_colonnes, est_credit

PATTERN_DEUX_CHIFFRES = re.compile(r'^\d{2}$')
PATTERN_ANNEE = re.compile(r'\b(20\d{2})\b')

# Mots-clés de repli si la détection de colonne échoue (page à une seule écriture)
MOTS_CLES_CREDIT = ('VIREMENT SRBM', 'VIR.RECU', 'VIR RECU', 'REMISE', 'ENCAISSEMENT', 'REGLEMENT DE VOTRE REMISE')


class BmceParser(BaseParser):
    NOM_BANQUE = "BMCE Bank Of Africa"

    def can_parse(self, texte_complet: str) -> bool:
        return (
            "COMPTES COURANTS COMMERCI" in texte_complet
            or "RELEVE CLIENT N" in texte_complet
        ) and "Attijariwafa" not in texte_complet and "C.I.H" not in texte_complet

    def _extraire_annee(self, texte: str) -> int:
        m = PATTERN_ANNEE.search(texte)
        return int(m.group(1)) if m else date.today().year

    def parse(self, chemin_pdf: str) -> list[Transaction]:
        nom_fichier = Path(chemin_pdf).name
        transactions = []

        with pdfplumber.open(chemin_pdf) as pdf:
            texte_complet = "\n".join(p.extract_text() or "" for p in pdf.pages)
            annee = self._extraire_annee(texte_complet)

            for page in pdf.pages:
                lignes = grouper_lignes(page)
                seuil = seuil_colonnes(lignes)

                for top in sorted(lignes):
                    mots = lignes[top]
                    r = extraire_montant_final(mots)
                    if not r:
                        continue
                    valeur, x1, idx_montant = r
                    avant = mots[:idx_montant]

                    if len(avant) < 4:
                        continue
                    if not (PATTERN_DEUX_CHIFFRES.match(avant[-2]['text']) and PATTERN_DEUX_CHIFFRES.match(avant[-1]['text'])):
                        continue
                    jour_val, mois_val = avant[-2]['text'], avant[-1]['text']
                    reste = avant[:-2]
                    if not (PATTERN_DEUX_CHIFFRES.match(reste[0]['text']) and PATTERN_DEUX_CHIFFRES.match(reste[1]['text'])):
                        continue
                    jour_op, mois_op = reste[0]['text'], reste[1]['text']
                    libelle = ' '.join(w['text'] for w in reste[2:]).strip()
                    if not libelle:
                        continue

                    try:
                        tx_date = date(annee, int(mois_val), int(jour_val))
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
