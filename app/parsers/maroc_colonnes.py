# parsers/maroc_colonnes.py
# Parseur pour relevés bancaires marocains à colonnes Débit/Crédit séparées par position
# (repéré via le mot-clé "DIRHAM" dans l'en-tête). Format observé (ex. "Relevé bancaire
# JUPITER FOX") : chaque mouvement est une ligne "DD MM {libellé} DD MM {montant}", le
# montant étant soit dans la colonne Débit (x≈400-460) soit Crédit (x≈460-520) — jamais
# les deux. Le libellé déborde parfois sur la ligne suivante (ex. numéro de RIB replié) :
# cette ligne de continuation n'a ni date en tête ni montant, elle est rattachée au
# mouvement précédent. Les lignes "SOLDE" (solde initial) et "REPORT" (sous-total reporté
# en haut de page suivante) sont ignorées : ce sont des repères de mise en page, pas des
# mouvements — le solde initial est déjà saisi côté formulaire d'import.

import re
from datetime import date
from pathlib import Path
from typing import Optional

import pdfplumber

from .base import BaseParser, Transaction, regrouper_lignes_par_position

RE_DATE_TETE = re.compile(r'^(\d{2})\s+(\d{2})$')
RE_ANNEE = re.compile(r'\b(20\d{2})\b')

X_DATE_VALEUR_MIN = 300
X_MONTANT_MIN = 345
X_SEPARATION_DEBIT_CREDIT = 460


def _normaliser_montant(texte: str) -> Optional[float]:
    """'15 000,00' / '7,00' -> float (espace = séparateur de milliers, virgule = décimale)."""
    t = re.sub(r"\s", "", texte or "")
    t = t.replace(",", ".")
    t = re.sub(r"[^\d.\-]", "", t)
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        return None


class MarocColonnesParser(BaseParser):
    NOM_BANQUE = "Relevé Maroc"

    def can_parse(self, texte_complet: str) -> bool:
        return "DIRHAM" in texte_complet.upper()

    def _deviner_annee(self, texte: str) -> int:
        m = RE_ANNEE.search(texte)
        return int(m.group(1)) if m else date.today().year

    def parse(self, chemin_pdf: str) -> list[Transaction]:
        nom_fichier = Path(chemin_pdf).name
        transactions: list[Transaction] = []

        with pdfplumber.open(chemin_pdf) as pdf:
            texte_complet = "\n".join(p.extract_text() or "" for p in pdf.pages)
            annee = self._deviner_annee(texte_complet)

            tx_courante: Optional[Transaction] = None

            for page in pdf.pages:
                mots = page.extract_words()
                if not mots:
                    continue

                for ligne in regrouper_lignes_par_position(mots):
                    tete = [w for w in ligne if w["x0"] < 70]
                    texte_tete = " ".join(w["text"] for w in tete).strip()
                    m_date = RE_DATE_TETE.match(texte_tete)

                    if m_date:
                        jour, mois = m_date.groups()
                        reste = [w for w in ligne if w["x0"] >= 70]
                        mots_libelle = [w for w in reste if w["x0"] < X_DATE_VALEUR_MIN]
                        mots_montant = [w for w in reste if w["x0"] >= X_MONTANT_MIN]
                        libelle = " ".join(w["text"] for w in mots_libelle).strip()

                        if not libelle or libelle.upper().startswith(("SOLDE", "REPORT")):
                            tx_courante = None
                            continue

                        if not mots_montant:
                            tx_courante = None
                            continue
                        valeur = _normaliser_montant(" ".join(w["text"] for w in mots_montant))
                        if valeur is None:
                            tx_courante = None
                            continue

                        try:
                            tx_date = date(annee, int(mois), int(jour))
                        except ValueError:
                            tx_courante = None
                            continue

                        est_credit = mots_montant[0]["x0"] >= X_SEPARATION_DEBIT_CREDIT
                        tx_courante = Transaction(
                            date=tx_date,
                            libelle=libelle,
                            debit=None if est_credit else valeur,
                            credit=valeur if est_credit else None,
                            solde=None,
                            banque=self.NOM_BANQUE,
                            fichier_source=nom_fichier,
                        )
                        transactions.append(tx_courante)

                    else:
                        # Ligne sans date en tête : continuation de libellé, ou ligne de
                        # sous-total/report purement numérique à ignorer (et qui rompt le
                        # rattachement, pour ne pas coller un total au mouvement précédent).
                        texte_ligne = " ".join(w["text"] for w in ligne).strip()
                        if not texte_ligne:
                            continue
                        if re.fullmatch(r"[\d\s.,]+", texte_ligne):
                            tx_courante = None
                            continue
                        if tx_courante is not None:
                            tx_courante.libelle = (tx_courante.libelle + " " + texte_ligne).strip()

        return transactions
