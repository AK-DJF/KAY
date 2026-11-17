# parsers/cih.py
# Parser pour les relevés CIH Bank (PDF natif, « RELEVE DE COMPTE BANCAIRE »).
# Format par ligne : JJ/MMJJ/MM <libellé> <montant>
# (date d'opération et date de valeur collées dans un seul mot, ex. "02/0202/02").
# Pas de mention Débit/Crédit par ligne : la colonne est déduite de la position x du
# montant par rapport à l'en-tête « CREDIT » du tableau.

import re
from datetime import date
from pathlib import Path

import pdfplumber

from .base import BaseParser, Transaction, deviner_annee_releve
from .util_position import extraire_montant_final, grouper_lignes

# "02/0202/02" (opération + valeur collées) ou "02/02" seul (valeur dans le mot suivant).
RE_DATES_COLLEES = re.compile(r'^(\d{2})/(\d{2})(\d{2})/(\d{2})$')
RE_DATE_SIMPLE = re.compile(r'^(\d{2})/(\d{2})$')


class CihParser(BaseParser):
    NOM_BANQUE = "CIH Bank"

    def can_parse(self, texte_complet: str) -> bool:
        texte = texte_complet.lower()
        return "cih.co.ma" in texte or "c.i.h siège" in texte or "c.i.h siege" in texte

    @staticmethod
    def _x_entete_credit(lignes: dict) -> float | None:
        """Bord droit de l'en-tête « CREDIT » : les montants au crédit se terminent à sa droite,
        ceux au débit nettement avant (colonne « DEBIT » plus à gauche)."""
        for mots in lignes.values():
            textes = [m['text'] for m in mots]
            if 'DEBIT' in textes and 'CREDIT' in textes:
                return next(m['x1'] for m in mots if m['text'] == 'CREDIT')
        return None

    def parse(self, chemin_pdf: str) -> list[Transaction]:
        nom_fichier = Path(chemin_pdf).name
        transactions = []

        with pdfplumber.open(chemin_pdf) as pdf:
            annee = deviner_annee_releve("\n".join(p.extract_text() or "" for p in pdf.pages))

            for page in pdf.pages:
                lignes = grouper_lignes(page)
                x_credit = self._x_entete_credit(lignes)
                if x_credit is None:
                    continue

                for top in sorted(lignes):
                    mots = lignes[top]
                    if not mots:
                        continue

                    m = RE_DATES_COLLEES.match(mots[0]['text'])
                    debut_libelle = 1
                    if m:
                        jour, mois = m.group(1), m.group(2)
                    else:
                        m = RE_DATE_SIMPLE.match(mots[0]['text'])
                        if not m:
                            continue
                        jour, mois = m.group(1), m.group(2)
                        if len(mots) > 1 and RE_DATE_SIMPLE.match(mots[1]['text']):
                            debut_libelle = 2

                    r = extraire_montant_final(mots)
                    if not r:
                        continue
                    valeur, x1, idx_montant = r
                    libelle = ' '.join(w['text'] for w in mots[debut_libelle:idx_montant]).strip()
                    if not libelle:
                        continue

                    try:
                        tx_date = date(annee, int(mois), int(jour))
                    except ValueError:
                        continue

                    credit = x1 > x_credit
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
