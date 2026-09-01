# parsers/generic.py
# Parser générique de fallback pour les banques non reconnues
# Stratégie 1 : pdfplumber extract_tables() (si le PDF a des bordures de tableau)
# Stratégie 2 : regex sur le texte brut (dates + montants)

import re
import pdfplumber
from datetime import date
from pathlib import Path
from typing import Optional
from .base import BaseParser, Transaction

# Formats de date fréquents dans les relevés français — ne capturent QUE la date (pas de
# `\s+(.*)` de fin) : ces patterns doivent matcher aussi bien une cellule de tableau isolée
# (juste la date, rien après) qu'une ligne de texte brut "date + reste" — un `\s+(.*)` de fin
# empêchait le 1er cas de matcher, et dans le 2e cas absorbait tout le reste de la ligne dans
# le match lui-même, faisant de `ligne[m.end():]` (le "reste" utilisé ensuite) une chaîne
# vide à chaque fois. Bug trouvé le 2026-08-20 sur un relevé Crédit Mutuel (0 mouvement
# extrait alors que le texte contenait bien des lignes "date + opération + montant").
PATTERNS_DATE = [
    re.compile(r'^(\d{2}/\d{2}/\d{4})'),      # DD/MM/YYYY
    re.compile(r'^(\d{2}/\d{2}/\d{2})(?!\d)'),  # DD/MM/YY
    re.compile(r'^(\d{2}/\d{2})(?!\d)'),        # DD/MM (sans année)
    re.compile(r'^(\d{4}-\d{2}-\d{2})'),        # YYYY-MM-DD
]

# Capture un nombre français quel que soit son séparateur de milliers (point ou espace) —
# la normalisation exacte (quel séparateur est la virgule décimale) se fait dans
# `_normaliser_montant`, pas ici : un regex à un seul type de séparateur toléré (comme
# l'ancienne version) coupait à tort "10.800,00" en "10.80" + "0,00" (même bug trouvé le
# 2026-08-20 sur le relevé Crédit Mutuel).
PATTERN_MONTANT = re.compile(
    r'([+-]?\d{1,3}(?:[ .]\d{3})+,\d{2}'   # 10.800,00 / 1 234,56
    r'|[+-]?\d+,\d{2}'                      # 35,57
    r'|[+-]?\d{1,3}(?:[ .]\d{3})+'          # 10.800 (sans décimales)
    r'|[+-]?\d+\.\d{2}'                     # 35.57
    r'|[+-]?\d+)\s*(?:EUR|€)?',
    re.IGNORECASE,
)


def _normaliser_montant(texte: str) -> Optional[float]:
    """Convertit '10.800,00', '1 234,56', '35,57', '35.57'... en float (dernier séparateur
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

PATTERN_PERIODE = re.compile(
    r'(\d{2}/\d{2}/(\d{4}))',
    re.IGNORECASE,
)


class GenericParser(BaseParser):
    NOM_BANQUE = "Banque inconnue"

    def can_parse(self, texte_complet: str) -> bool:
        # Accepte tout — c'est le fallback de dernier recours
        return True

    def _deviner_annee(self, texte: str) -> int:
        m = PATTERN_PERIODE.search(texte)
        if m:
            return int(m.group(2))
        return date.today().year

    def _parser_via_tables(self, pdf, nom_fichier: str, annee: int) -> list[Transaction]:
        """Tente d'extraire les transactions via la détection automatique de tableaux."""
        transactions = []

        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                for row in table:
                    if not row or all(c is None for c in row):
                        continue
                    # Chercher une date dans la première colonne non vide
                    premiere_col = next((c for c in row if c), "")
                    tx = self._parser_ligne_table(premiere_col, row, nom_fichier, annee)
                    if tx:
                        transactions.append(tx)

        return transactions

    def _parser_ligne_table(self, date_str: str, row: list, nom_fichier: str, annee: int) -> Optional[Transaction]:
        """Essaie de construire une Transaction depuis une ligne de tableau."""
        # Chercher une date valide
        for pat in PATTERNS_DATE:
            m = pat.match((date_str or "").strip())
            if m:
                break
        else:
            return None

        try:
            tx_date = self._convertir_date(date_str.strip(), annee)
        except Exception:
            return None

        # Chercher libellé et montants dans les autres colonnes
        cols = [str(c or "").strip() for c in row]
        libelle = cols[1] if len(cols) > 1 else ""

        debit, credit, solde = None, None, None
        montants = []
        for col in cols[2:]:
            m_montant = PATTERN_MONTANT.search(col)
            if m_montant:
                valeur = _normaliser_montant(m_montant.group(1))
                if valeur is not None:
                    montants.append(valeur)

        # Heuristique : 2 colonnes montant = débit + crédit, 1 = débit ou crédit selon le signe
        if len(montants) >= 2:
            debit = abs(montants[0]) if montants[0] < 0 else None
            credit = abs(montants[0]) if montants[0] > 0 else None
            if debit is None and montants[1] < 0:
                debit = abs(montants[1])
            if credit is None and montants[1] > 0:
                credit = abs(montants[1])
        elif len(montants) == 1:
            if montants[0] < 0:
                debit = abs(montants[0])
            else:
                credit = abs(montants[0])

        if debit is None and credit is None:
            return None

        return Transaction(
            date=tx_date,
            libelle=libelle,
            debit=debit,
            credit=credit,
            solde=solde,
            banque=self.NOM_BANQUE,
            fichier_source=nom_fichier,
        )

    def _parser_via_texte(self, pdf, nom_fichier: str, annee: int) -> list[Transaction]:
        """Fallback : regex ligne par ligne sur le texte brut."""
        transactions = []

        toutes_lignes = []
        for page in pdf.pages:
            texte = page.extract_text() or ""
            toutes_lignes.extend(texte.split("\n"))

        for ligne in toutes_lignes:
            ligne = ligne.strip()
            if not ligne:
                continue

            tx_date = None
            reste = ligne

            for pat in PATTERNS_DATE:
                m = pat.match(ligne)
                if m:
                    try:
                        tx_date = self._convertir_date(m.group(1), annee)
                        reste = ligne[m.end():].strip()
                    except Exception:
                        pass
                    break

            if tx_date is None:
                continue

            # Extraire tous les montants de la ligne (findall renvoie des chaînes, pas des
            # tuples : un seul groupe capturant dans PATTERN_MONTANT — indexer avec [0]
            # dessus prenait par erreur le 1er caractère du montant, pas le montant entier).
            montants_trouves = PATTERN_MONTANT.findall(reste)
            if not montants_trouves:
                continue

            # Le dernier montant trouvé est généralement le bon
            valeur = _normaliser_montant(montants_trouves[-1])
            if valeur is None:
                continue

            libelle = PATTERN_MONTANT.sub("", reste).strip()
            libelle = re.sub(r'\s{2,}', ' ', libelle).strip()

            debit = abs(valeur) if valeur < 0 else None
            credit = abs(valeur) if valeur > 0 else None

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

    def _convertir_date(self, date_str: str, annee_defaut: int) -> date:
        date_str = date_str.strip()
        if re.match(r'^\d{2}/\d{2}/\d{4}$', date_str):
            j, m, a = date_str.split('/')
            return date(int(a), int(m), int(j))
        if re.match(r'^\d{2}/\d{2}/\d{2}$', date_str):
            j, m, a = date_str.split('/')
            return date(2000 + int(a), int(m), int(j))
        if re.match(r'^\d{2}/\d{2}$', date_str):
            j, m = date_str.split('/')
            return date(annee_defaut, int(m), int(j))
        if re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
            a, m, j = date_str.split('-')
            return date(int(a), int(m), int(j))
        raise ValueError(f"Format de date non reconnu : {date_str}")

    def parse(self, chemin_pdf: str) -> list[Transaction]:
        nom_fichier = Path(chemin_pdf).name

        with pdfplumber.open(chemin_pdf) as pdf:
            texte_complet = "\n".join(p.extract_text() or "" for p in pdf.pages)
            annee = self._deviner_annee(texte_complet)

            # Essai 1 : extraction par tableaux
            transactions = self._parser_via_tables(pdf, nom_fichier, annee)

            # Essai 2 : fallback texte si le tableau n'a rien donné
            if not transactions:
                transactions = self._parser_via_texte(pdf, nom_fichier, annee)

        return transactions



