# parsers/bmce.py
# Parser pour les relevés BMCE Bank of Africa (PDF natif) — calé sur 2 relevés réels
# (juillet 2026). Contrairement à Attijariwafa, l'en-tête de colonnes de ce relevé est une
# image (pas du texte) et le tableau n'a pas de bordures détectables par
# pdfplumber.extract_tables() : on travaille donc directement sur les mots positionnés
# (extract_words), en repérant les colonnes par la structure de la ligne plutôt que par
# une position en pixels fixe (l'échelle varie légèrement d'une page à l'autre du même
# relevé, ~2%, ce qui rend des seuils de position absolus peu fiables).
#
# Structure d'une ligne de mouvement : [JJ] [MM] (date opération, sans année) puis un
# libellé de longueur variable, puis [JJ] [MM] (date valeur, sans année) puis un montant —
# les gros montants sont coupés par pdfplumber en plusieurs mots au niveau des séparateurs
# de milliers (ex. "1 528 872,94" -> mots "1"/"528"/"872,94"). On reconstruit ce montant en
# remontant depuis la fin de la ligne : le dernier mot doit être décimal (ex. "872,94"), et
# on rattache les mots précédents tant qu'ils sont de purs groupes de chiffres très proches
# horizontalement (écart < 20pt, propre à un même nombre) — le grand écart suivant marque
# la fin du montant et le début (en remontant) de la date valeur.
# Débit ou crédit est déterminé par l'écart horizontal entre le début du montant et la date
# valeur : ~65-70pt pour la colonne Débit, ~140-160pt pour la colonne Crédit (relevés
# calibrés) — seuil à 100pt entre les deux, invariant à l'échelle car relatif à la ligne.
# L'année (absente des dates de la table) est reprise de la période du relevé
# ("PAGE X/Y JJ MM AAAA JJ MM AAAA" en haut de la 1ère page).

import re
from datetime import date
from pathlib import Path
from typing import Optional

import pdfplumber

from .base import BaseParser, Transaction, regrouper_lignes_par_position

RE_2CHIFFRES = re.compile(r'^\d{2}$')
RE_MONTANT_FIN = re.compile(r'^\d+,\d{2}$')
RE_GROUPE_MILLIERS = re.compile(r'^\d{1,3}$')
RE_PERIODE = re.compile(r'(\d{2})\s(\d{2})\s(\d{4})\s+\d{2}\s\d{2}\s\d{4}')


def _normaliser_montant(texte: str) -> Optional[float]:
    t = (texte or "").replace(" ", "").replace(",", ".")
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        return None


class BMCEBankOfAfricaParser(BaseParser):
    NOM_BANQUE = "BMCE Bank of Africa"

    def can_parse(self, texte_complet: str) -> bool:
        texte = texte_complet.upper()
        if "BMCE" in texte or "BANK OF AFRICA" in texte:
            return True
        # Le logo/pied de page portant "BMCE"/"BANK OF AFRICA" est parfois une image, sans
        # texte extractible (relevé "RELEVE CLIENT" précédé d'une page de rapport
        # d'impression) — "011 780" (code banque BMCE + code ville Casablanca du RIB) reste
        # présent en texte en en-tête de compte sur ce gabarit, et sert de repli.
        return "011 780" in texte_complet and "COMPTES COURANTS" in texte

    def _annee_periode(self, pdf) -> int:
        for page in pdf.pages[:2]:
            texte = page.extract_text() or ""
            m = RE_PERIODE.search(texte)
            if m:
                return int(m.group(3))
        return date.today().year

    def parse(self, chemin_pdf: str) -> list[Transaction]:
        nom_fichier = Path(chemin_pdf).name
        transactions: list[Transaction] = []

        with pdfplumber.open(chemin_pdf) as pdf:
            annee = self._annee_periode(pdf)
            for page in pdf.pages:
                words = page.extract_words(x_tolerance=1)
                for ligne in regrouper_lignes_par_position(words):
                    tx = self._parser_ligne(ligne, annee, nom_fichier)
                    if tx:
                        transactions.append(tx)

        return transactions

    def _parser_ligne(self, ligne: list[dict], annee: int, nom_fichier: str) -> Optional[Transaction]:
        if len(ligne) < 5:
            return None
        if not (RE_2CHIFFRES.match(ligne[0]["text"]) and RE_2CHIFFRES.match(ligne[1]["text"])):
            return None
        jour_op, mois_op = ligne[0]["text"], ligne[1]["text"]

        if not RE_MONTANT_FIN.match(ligne[-1]["text"]):
            return None

        idx = len(ligne) - 1
        montant_mots = [ligne[idx]]
        while (idx - 1 >= 4 and RE_GROUPE_MILLIERS.match(ligne[idx - 1]["text"])
               and (ligne[idx]["x0"] - ligne[idx - 1]["x0"]) <= 20):
            idx -= 1
            montant_mots.insert(0, ligne[idx])

        idx_valeur_jour = idx - 2
        if idx_valeur_jour < 2:
            return None
        valeur_jour_w, valeur_mois_w = ligne[idx_valeur_jour], ligne[idx_valeur_jour + 1]
        if not (RE_2CHIFFRES.match(valeur_jour_w["text"]) and RE_2CHIFFRES.match(valeur_mois_w["text"])):
            return None

        libelle = " ".join(w["text"] for w in ligne[2:idx_valeur_jour])
        montant = _normaliser_montant("".join(w["text"] for w in montant_mots))
        if montant is None:
            return None

        try:
            date_op = date(annee, int(mois_op), int(jour_op))
        except ValueError:
            return None

        gap = montant_mots[0]["x0"] - valeur_mois_w["x0"]
        est_credit = gap >= 100

        return Transaction(
            date=date_op, libelle=libelle,
            debit=None if est_credit else montant,
            credit=montant if est_credit else None,
            solde=None, banque=self.NOM_BANQUE, fichier_source=nom_fichier,
        )
