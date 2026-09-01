# parsers/societe_generale.py
# Parser pour les relevés Société Générale (PDF natif)
# Calé sur un relevé réel YARA CONNECT (février 2025) — voir
# tests/releves/societe-generale/exemple-01.pdf + exemple-01.attendu.md.
#
# Particularité de ce gabarit : page.extract_text() par défaut ne restitue aucun
# espace entre les mots (police/PDF sans espaces explicites) — un x_tolerance très
# serré (1.0) est nécessaire pour reconstituer un texte lisible. De plus, le
# relevé n'affiche qu'UN SEUL montant par ligne d'opération (pas de séparation
# explicite débit/crédit dans le texte) : la colonne (Débit ou Crédit) est
# déterminée par la position horizontale du montant, comparée à celle des
# en-têtes de colonnes "Débit"/"Crédit" repérés sur la page.

import re
from datetime import date
from pathlib import Path
from typing import Optional

import pdfplumber

from .base import BaseParser, Transaction, regrouper_lignes_par_position

PATTERN_LIGNE = re.compile(
    r'^(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+(.+?)\s+([\d.]+,\d{2})\s*\*?$'
)
PATTERN_MONTANT_MOT = re.compile(r'^[\d.]+,\d{2}$')


class SocieteGeneraleParser(BaseParser):
    NOM_BANQUE = "Société Générale"

    def can_parse(self, texte_complet: str) -> bool:
        # "552120222" = SIREN de Société Générale, présent sur tous ses relevés,
        # insensible au bug d'espaces manquants (chiffres toujours contigus).
        if "552120222" in texte_complet:
            return True
        texte = texte_complet.upper()
        if "MAROC" in texte:
            return False  # Société Générale Maroc — voir parsers/maroc.py
        return "SOCIETE GENERALE" in texte or "SOCIÉTÉ GÉNÉRALE" in texte

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
                    date_txt, _valeur_txt, description, montant_txt = m.groups()

                    mot_montant = next((w for w in ligne_mots if w["text"] == montant_txt), None)
                    if mot_montant is None:
                        # Repli si la correspondance exacte échoue : dernier mot au format montant de la ligne
                        candidats = [w for w in ligne_mots if PATTERN_MONTANT_MOT.match(w["text"])]
                        mot_montant = candidats[-1] if candidats else None
                    if mot_montant is None:
                        continue

                    montant = self._montant(montant_txt)
                    est_credit = mot_montant["x0"] >= milieu

                    try:
                        jour, mois, annee = (int(x) for x in date_txt.split("/"))
                        date_operation = date(annee, mois, jour)
                    except ValueError:
                        continue

                    transactions.append(Transaction(
                        date=date_operation, libelle=description.strip(),
                        debit=None if est_credit else montant,
                        credit=montant if est_credit else None,
                        solde=None, banque=self.NOM_BANQUE, fichier_source=nom_fichier,
                    ))

        return transactions

