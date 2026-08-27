# parsers/bred.py
# Parser pour les relevés BRED Banque Populaire (PDF natif)
# Calé sur un relevé réel HTC RENOV (mars 2026, 4 pages) — voir
# tests/releves/bred/exemple-01.pdf + exemple-01.attendu.md.
#
# Format d'une ligne d'opération : "DD.MM Nature[+référence] montant DD.MM.YY"
# (une seule colonne de montant dans le texte brut, comme Société Générale) —
# la colonne (Débit ou Crédit) est déterminée par la position horizontale du
# montant, comparée aux en-têtes de colonnes "Débit"/"Crédit" repérés sur la page.

import re
from datetime import date
from pathlib import Path
from typing import Optional

import pdfplumber

from .base import BaseParser, Transaction, regrouper_lignes_par_position

PATTERN_LIGNE = re.compile(
    r'^(\d{2})\.(\d{2})\s+(.+?)\s+([\d.]+,\d{2})\s+\d{2}\.\d{2}\.(\d{2})$'
)
PATTERN_MONTANT_MOT = re.compile(r'^[\d.]+,\d{2}$')


class BREDParser(BaseParser):
    NOM_BANQUE = "BRED"

    def can_parse(self, texte_complet: str) -> bool:
        texte = texte_complet.upper()
        return "BRED" in texte and ("BANQUE POPULAIRE" in texte or "BREDFRPP" in texte)

    def _montant(self, texte: str) -> float:
        return float(texte.replace(".", "").replace(",", "."))

    def parse(self, chemin_pdf: str) -> list[Transaction]:
        nom_fichier = Path(chemin_pdf).name
        transactions = []

        with pdfplumber.open(chemin_pdf) as pdf:
            x_debit, x_credit = None, None
            for page in pdf.pages:
                words = page.extract_words(x_tolerance=1)

                for w in words:
                    if w["text"] == "Débit":
                        x_debit = w["x0"]
                    elif w["text"] == "Crédit":
                        x_credit = w["x0"]

                if x_debit is None or x_credit is None:
                    continue  # page sans en-tête de colonnes exploitable
                milieu = (x_debit + x_credit) / 2

                for ligne_mots in regrouper_lignes_par_position(words):
                    texte_ligne = " ".join(w["text"] for w in ligne_mots)
                    m = PATTERN_LIGNE.match(texte_ligne)
                    if not m:
                        continue
                    jour, mois, description, montant_txt, annee_2ch = m.groups()

                    mot_montant = next((w for w in ligne_mots if w["text"] == montant_txt), None)
                    if mot_montant is None:
                        candidats = [w for w in ligne_mots if PATTERN_MONTANT_MOT.match(w["text"])]
                        mot_montant = candidats[-1] if candidats else None
                    if mot_montant is None:
                        continue

                    montant = self._montant(montant_txt)
                    est_credit = mot_montant["x0"] >= milieu

                    try:
                        # "Date" (1re colonne) n'a pas d'année -> reprise de l'année à 2 chiffres
                        # de la colonne "Valeur" du même relevé (mois/jour identiques dans ce gabarit).
                        annee = 2000 + int(annee_2ch)
                        date_operation = date(annee, int(mois), int(jour))
                    except ValueError:
                        continue

                    transactions.append(Transaction(
                        date=date_operation, libelle=description.strip(),
                        debit=None if est_credit else montant,
                        credit=montant if est_credit else None,
                        solde=None, banque=self.NOM_BANQUE, fichier_source=nom_fichier,
                    ))

        return transactions
