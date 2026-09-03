# services/extractor.py
# Orchestre l'extraction : reçoit un chemin PDF, retourne des Transaction[]

from pathlib import Path
from parsers.detector import detecter_parser
from parsers.base import Transaction
from parsers.generic import GenericParser
from services.extractor_ia import extraire_transactions_ia, ExtractionIAError, OPENROUTER_API_KEY


def extraire_transactions(chemin_pdf: str) -> tuple[list[Transaction], str]:
    """
    Extrait les transactions d'un PDF de relevé bancaire. Priorité au parseur local dédié
    (parsers/detector.py) quand la banque est reconnue : ces parseurs sont calés et
    vérifiés champ par champ sur des relevés réels de chaque banque (totaux exacts), alors
    que l'IA de vision peut se tromper sur des montants (moins fiable sur un document dense
    en chiffres) — mieux vaut le déterministe quand il est disponible. L'IA
    (extraire_transactions_ia, OPENROUTER_API_KEY requise) n'est tentée qu'en repli, pour
    une banque non calibrée (GenericParser) ou si le parseur dédié n'a rien extrait.
    Retourne (transactions, nom_banque).
    """
    if not Path(chemin_pdf).exists():
        raise FileNotFoundError(f"Fichier introuvable : {chemin_pdf}")

    parser = detecter_parser(chemin_pdf)
    transactions: list[Transaction] = []
    if not isinstance(parser, GenericParser):
        transactions = parser.parse(chemin_pdf)
        if transactions:
            return transactions, parser.NOM_BANQUE

    erreur_ia: str | None = None
    if OPENROUTER_API_KEY:
        try:
            return extraire_transactions_ia(chemin_pdf)
        except ExtractionIAError as e:
            erreur_ia = str(e)

    if isinstance(parser, GenericParser):
        transactions = parser.parse(chemin_pdf)
    if not transactions and erreur_ia:
        raise RuntimeError(
            f"Aucun mouvement extrait. Extraction IA échouée ({erreur_ia}) et banque "
            f"non reconnue par les parseurs locaux."
        )
    return transactions, parser.NOM_BANQUE

