# parsers/base.py
# Contrat commun à tous les parsers de relevés bancaires

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class Transaction:
    date: date
    libelle: str
    debit: Optional[float]
    credit: Optional[float]
    solde: Optional[float]
    banque: str
    fichier_source: str
    # Champs enrichis après parsing
    categorie: str = "Non catégorisé"
    annee: int = field(init=False)
    mois: int = field(init=False)

    def __post_init__(self):
        self.annee = self.date.year
        self.mois = self.date.month

    @property
    def montant_net(self) -> float:
        """Débit = négatif, crédit = positif."""
        return (self.credit or 0.0) - (self.debit or 0.0)

    def to_dict(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "libelle": self.libelle,
            "debit": self.debit,
            "credit": self.credit,
            "solde": self.solde,
            "banque": self.banque,
            "fichier_source": self.fichier_source,
            "categorie": self.categorie,
            "annee": self.annee,
            "mois": self.mois,
        }


class BaseParser(ABC):
    """Classe abstraite — chaque banque implémente ses propres règles."""

    NOM_BANQUE: str = "Inconnu"

    @abstractmethod
    def can_parse(self, texte_complet: str) -> bool:
        """Retourne True si ce parser reconnaît le format du PDF."""

    @abstractmethod
    def parse(self, chemin_pdf: str) -> list[Transaction]:
        """Extrait la liste des transactions depuis le PDF."""


def regrouper_lignes_par_position(words: list[dict], seuil: float = 2.0) -> list[list[dict]]:
    """
    Regroupe les mots d'une page (issus de page.extract_words) en lignes visuelles,
    en se basant sur leur position verticale ('top') plutôt que sur le texte brut.
    Nécessaire pour les PDF dont extract_text() ne restitue pas de colonnes exploitables
    par une simple regex (ex. Société Générale, BRED) — chaque ligne retournée est triée
    par position horizontale ('x0'), prête à être jointe en texte ou inspectée mot par mot
    (utile pour déterminer, via la position d'un montant, à quelle colonne il appartient).
    """
    lignes: list[list[dict]] = []
    ligne_courante: list[dict] = []
    top_courant: Optional[float] = None

    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if top_courant is None or abs(w["top"] - top_courant) <= seuil:
            ligne_courante.append(w)
            top_courant = w["top"] if top_courant is None else top_courant
        else:
            lignes.append(sorted(ligne_courante, key=lambda w: w["x0"]))
            ligne_courante = [w]
            top_courant = w["top"]

    if ligne_courante:
        lignes.append(sorted(ligne_courante, key=lambda w: w["x0"]))

    return lignes
