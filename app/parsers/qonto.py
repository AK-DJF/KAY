# parsers/qonto.py
# Parser pour les relevés Qonto (PDF natif)
# Format : Date de valeur | Transactions | Débit | Crédit
# Les dates sont au format JJ/MM — l'année est extraite de la période du relevé

import re
import pdfplumber
from datetime import date
from pathlib import Path
from typing import Optional
from .base import BaseParser, Transaction

# Priorité 1 : montant suivi explicitement de EUR
PATTERN_MONTANT_EUR = re.compile(
    r'([+-])\s*([\d][\d\s]*[.,][\d]{2})\s*EUR',
    re.IGNORECASE,
)
# Priorité 2 : montant sans devise (ou devise inconnue) — fallback
PATTERN_MONTANT = re.compile(
    r'([+-])\s*([\d][\d\s]*[.,][\d]{2})\s*(?!GBP|USD|CHF|JPY|CAD)(?:[A-Z]{3})?',
    re.IGNORECASE,
)
PATTERN_DATE = re.compile(r'^(\d{2}/\d{2})\s+(.*)', re.DOTALL)
PATTERN_PERIODE = re.compile(r'Du\s+\d{2}/\d{2}/(\d{4})', re.IGNORECASE)

# Lignes à ignorer (en-têtes, pieds de page, récapitulatifs)
LIGNES_IGNOREES = re.compile(
    r'^(Date de valeur|Transactions|D[ée]bit|Cr[ée]dit|Solde au|Entr[ée]es|Sorties'
    r'|Yara Connect|Du \d|IBAN|BIC|\d+/\d+ *$|Toutes les cartes|Qonto)',
    re.IGNORECASE,
)


class QontoParser(BaseParser):
    NOM_BANQUE = "Qonto"

    def can_parse(self, texte_complet: str) -> bool:
        return "QNTOFRP1XXX" in texte_complet or (
            "Qonto" in texte_complet and "Date de valeur" in texte_complet
        )

    def _extraire_annee(self, texte: str) -> int:
        m = PATTERN_PERIODE.search(texte)
        return int(m.group(1)) if m else date.today().year

    def _nettoyer_montant(self, texte: str) -> tuple[Optional[float], Optional[float]]:
        """Extrait (debit, credit). Préfère les montants en EUR, ignore les devises étrangères."""
        # Chercher d'abord un montant explicitement en EUR
        m = PATTERN_MONTANT_EUR.search(texte)
        if not m:
            m = PATTERN_MONTANT.search(texte)
        if not m:
            return None, None
        signe = m.group(1)
        valeur = float(m.group(2).replace(' ', '').replace(',', '.'))
        return (valeur, None) if signe == '-' else (None, valeur)

    def parse(self, chemin_pdf: str) -> list[Transaction]:
        transactions = []
        nom_fichier = Path(chemin_pdf).name

        with pdfplumber.open(chemin_pdf) as pdf:
            # Concaténer le texte de toutes les pages pour l'extraction de l'année
            texte_complet = "\n".join(p.extract_text() or "" for p in pdf.pages)
            annee = self._extraire_annee(texte_complet)

            # Collecter toutes les lignes de toutes les pages
            toutes_lignes: list[str] = []
            for page in pdf.pages:
                texte = page.extract_text() or ""
                toutes_lignes.extend(texte.split("\n"))

        # Construire les blocs de transactions
        # Un bloc = [ligne_date, *lignes_suivantes] jusqu'à la prochaine date
        blocs: list[list[str]] = []
        bloc_courant: list[str] = []

        for ligne in toutes_lignes:
            ligne = ligne.strip()
            if not ligne or LIGNES_IGNOREES.match(ligne):
                if bloc_courant:
                    blocs.append(bloc_courant)
                    bloc_courant = []
                continue

            if PATTERN_DATE.match(ligne):
                if bloc_courant:
                    blocs.append(bloc_courant)
                bloc_courant = [ligne]
            elif bloc_courant:
                bloc_courant.append(ligne)

        if bloc_courant:
            blocs.append(bloc_courant)

        # Parser chaque bloc
        for bloc in blocs:
            if not bloc:
                continue

            m_date = PATTERN_DATE.match(bloc[0])
            if not m_date:
                continue

            date_str = m_date.group(1)
            premiere_partie = m_date.group(2).strip()

            # Assembler le texte complet du bloc
            parties = [premiere_partie] + bloc[1:]
            texte_bloc = " ".join(p for p in parties if p)

            # Chercher le montant EUR en priorité (en partant de la fin du bloc)
            debit, credit = None, None
            libelle_parties = list(parties)

            # Passe 1 : chercher un montant EUR explicite
            for k in range(len(parties) - 1, -1, -1):
                m_eur = PATTERN_MONTANT_EUR.search(parties[k])
                if m_eur:
                    signe = m_eur.group(1)
                    valeur = float(m_eur.group(2).replace(' ', '').replace(',', '.'))
                    debit, credit = (valeur, None) if signe == '-' else (None, valeur)
                    texte_sans_montant = PATTERN_MONTANT_EUR.sub("", parties[k]).strip()
                    libelle_parties[k] = texte_sans_montant
                    break

            # Passe 2 : fallback si aucun montant EUR trouvé
            if debit is None and credit is None:
                for k in range(len(parties) - 1, -1, -1):
                    d, c = self._nettoyer_montant(parties[k])
                    if d is not None or c is not None:
                        debit, credit = d, c
                        texte_sans_montant = PATTERN_MONTANT.sub("", parties[k]).strip()
                        libelle_parties[k] = texte_sans_montant
                        break

            if debit is None and credit is None:
                continue

            # Construire le libellé (sans montant ni mentions EUR)
            libelle = " ".join(p for p in libelle_parties if p).strip()
            libelle = re.sub(r'\s{2,}', ' ', libelle)

            jour, mois = map(int, date_str.split("/"))
            try:
                tx_date = date(annee, mois, jour)
            except ValueError:
                continue

            transactions.append(Transaction(
                date=tx_date,
                libelle=libelle,
                debit=debit,
                credit=credit,
                solde=None,
                banque=self.NOM_BANQUE,
                fichier_source=nom_fichier,
            ))

        return transactions

