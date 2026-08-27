# services/extractor.py
# Orchestre l'extraction : reçoit un chemin PDF, retourne des Transaction[]

from pathlib import Path
from parsers.detector import detecter_parser
from parsers.base import Transaction


def extraire_transactions(chemin_pdf: str) -> tuple[list[Transaction], str]:
    """
    Extrait les transactions d'un PDF de relevé bancaire.
    Retourne (transactions, nom_banque).
    """
    if not Path(chemin_pdf).exists():
        raise FileNotFoundError(f"Fichier introuvable : {chemin_pdf}")

    parser = detecter_parser(chemin_pdf)
    transactions = parser.parse(chemin_pdf)
    return transactions, parser.NOM_BANQUE
