# services/extractor.py
# Orchestre l'extraction : reçoit un chemin PDF, retourne des Transaction[]

from pathlib import Path
from parsers.detector import detecter_parser
from parsers.base import Transaction
from services.extractor_ia import extraire_transactions_ia, ExtractionIAError, OPENROUTER_API_KEY


def extraire_transactions(chemin_pdf: str) -> tuple[list[Transaction], str]:
    """
    Extrait les transactions d'un PDF de relevé bancaire. Si OPENROUTER_API_KEY est
    configurée, tente d'abord l'extraction par IA de vision (extraire_transactions_ia) —
    fonctionne sur n'importe quelle banque, y compris non calibrée par un parseur dédié
    ci-dessous, et sur les scans de mauvaise qualité. En cas d'échec (ou si aucune clé
    n'est configurée), retombe sur les parseurs locaux (parsers/detector.py — texte natif
    ou OCR selon le PDF).
    Retourne (transactions, nom_banque).
    """
    if not Path(chemin_pdf).exists():
        raise FileNotFoundError(f"Fichier introuvable : {chemin_pdf}")

    erreur_ia: str | None = None
    if OPENROUTER_API_KEY:
        try:
            return extraire_transactions_ia(chemin_pdf)
        except ExtractionIAError as e:
            erreur_ia = str(e)  # repli sur les parseurs locaux ci-dessous, mais on garde
            # la raison — utile si le repli échoue aussi (voir plus bas)

    parser = detecter_parser(chemin_pdf)
    transactions = parser.parse(chemin_pdf)
    if not transactions and erreur_ia:
        raise RuntimeError(
            f"Aucun mouvement extrait. Extraction IA échouée ({erreur_ia}) et banque "
            f"non reconnue par les parseurs locaux."
        )
    return transactions, parser.NOM_BANQUE

