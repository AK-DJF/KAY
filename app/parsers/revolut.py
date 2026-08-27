# parsers/revolut.py
# Parser pour les relevés Revolut Business (PDF natif)
# Calé sur un relevé réel MUSC-PAY (janvier-juillet 2026, 34 pages) — voir
# tests/releves/revolut/exemple-01.pdf + exemple-01.attendu.md.
#
# Format : "DD mmm. YYYY TYPE Description €montant €solde" par ligne de transaction
# (TYPE = CAR/MOA/MOS/MOR/FEE/EXI/EXO/ATM). Une seule colonne de montant apparaît par
# ligne (le PDF a deux colonnes "Argent sortant"/"Argent entrant" mais une seule est
# remplie) : le sens (débit/crédit) n'est donc pas lisible directement sur la ligne.
#
# Le relevé liste les transactions de la plus récente à la plus ancienne, avec le
# solde courant après chaque opération. On détermine le sens en comparant le solde
# de chaque ligne à celui de la ligne suivante (plus ancienne) : si le solde diminue
# en remontant dans le temps, l'opération était un crédit (le solde était plus bas
# avant), et inversement. Pour la toute dernière ligne (la plus ancienne), on compare
# au "Solde d'ouverture" du résumé en tête de relevé.

import re
from datetime import date
from pathlib import Path
from typing import Optional

import pdfplumber

from .base import BaseParser, Transaction

MOIS_FR = {
    "janv": 1, "févr": 2, "mars": 3, "avr": 4, "mai": 5, "juin": 6,
    "juil": 7, "août": 8, "sept": 9, "oct": 10, "nov": 11, "déc": 12,
}

PATTERN_LIGNE = re.compile(
    r'^(\d{1,2})\s+(janv|févr|mars|avr|mai|juin|juil|août|sept|oct|nov|déc)\.?\s+'
    r'(\d{4})\s+([A-Z]{3})\s+(.*)$'
)
PATTERN_MONTANT_EUR = re.compile(r'€\s*([\d  ]+[.,]\d{2})')
PATTERN_SOLDE_OUVERTURE = re.compile(r"Solde d.ouverture\s*€\s*([\d  ]+[.,]\d{2})")

CATEGORIES = {
    "CAR": "Carte bancaire", "MOA": "Virement entrant", "MOS": "Virement sortant",
    "MOR": "Remboursement", "FEE": "Frais Revolut", "EXI": "Change (crédit)",
    "EXO": "Change (débit)", "ATM": "Retrait DAB",
}


class RevolutParser(BaseParser):
    NOM_BANQUE = "Revolut"

    def can_parse(self, texte_complet: str) -> bool:
        return "revolut" in texte_complet.lower()

    def _montant(self, texte: str) -> float:
        return float(texte.replace(" ", "").replace(" ", "").replace(",", "."))

    def parse(self, chemin_pdf: str) -> list[Transaction]:
        nom_fichier = Path(chemin_pdf).name

        with pdfplumber.open(chemin_pdf) as pdf:
            toutes_lignes: list[str] = []
            for page in pdf.pages:
                texte = page.extract_text() or ""
                toutes_lignes.extend(texte.split("\n"))

        texte_complet = "\n".join(toutes_lignes)
        m_ouverture = PATTERN_SOLDE_OUVERTURE.search(texte_complet)
        if not m_ouverture:
            raise ValueError("Solde d'ouverture introuvable dans le relevé Revolut — format inattendu")
        solde_ouverture = self._montant(m_ouverture.group(1))

        # Passe 1 : extraire chaque ligne de transaction brute (plus récente -> plus ancienne)
        brutes: list[dict] = []
        for ligne in toutes_lignes:
            ligne = ligne.strip()
            m = PATTERN_LIGNE.match(ligne)
            if not m:
                continue
            jour, mois_txt, annee, type_code, reste = m.groups()
            mois = MOIS_FR.get(mois_txt)
            if not mois:
                continue

            m_premier_montant = PATTERN_MONTANT_EUR.search(reste)
            montants = PATTERN_MONTANT_EUR.findall(reste)
            if not m_premier_montant or len(montants) < 2:
                continue

            description = reste[:m_premier_montant.start()].strip(" •").strip()
            montant = self._montant(montants[-2])
            solde_apres = self._montant(montants[-1])

            try:
                jour_date = date(int(annee), mois, int(jour))
            except ValueError:
                continue

            brutes.append({
                "date": jour_date, "type": type_code, "description": description,
                "montant": montant, "solde_apres": solde_apres,
            })

        # Passe 2 : déterminer le sens (débit/crédit) par comparaison de soldes successifs
        transactions = []
        for i, b in enumerate(brutes):
            solde_avant = brutes[i + 1]["solde_apres"] if i + 1 < len(brutes) else solde_ouverture
            diff = round(b["solde_apres"] - solde_avant, 2)
            if diff > 0:
                debit, credit = None, b["montant"]
            elif diff < 0:
                debit, credit = b["montant"], None
            else:
                continue

            transactions.append(Transaction(
                date=b["date"], libelle=b["description"] or b["type"],
                debit=debit, credit=credit, solde=b["solde_apres"],
                banque=self.NOM_BANQUE, fichier_source=nom_fichier,
                categorie=CATEGORIES.get(b["type"], "Non catégorisé"),
            ))

        return transactions
