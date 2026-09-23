# parsers/detector.py
# Détecte automatiquement la banque et retourne le bon parser

import pdfplumber
from .base import BaseParser
from .qonto import QontoParser
from .revolut import RevolutParser
from .societe_generale import SocieteGeneraleParser
from .bred import BREDParser
from .credit_mutuel import CreditMutuelParser
from .maroc_colonnes import MarocColonnesParser
from .attijariwafa_codes import AttijariwafaCodesParser
from .bmce import BmceParser
from .saham import SahamParser
from .cfg_bank import CfgBankParser
from .generic import GenericParser

# Liste ordonnée des parsers spécifiques (du plus précis au plus générique).
# Qonto, Revolut, Société Générale, BRED, Crédit Mutuel et Maroc (colonnes) sont calibrés
# sur des relevés réels. AttijariwafaCodesParser et BmceParser doivent passer avant
# MarocColonnesParser : les trois formats contiennent "DIRHAM" dans l'en-tête, mais les
# deux premiers ont une signature plus spécifique (nom de banque + motif de colonnes propre).
PARSERS_DISPONIBLES: list[BaseParser] = [
    QontoParser(),
    RevolutParser(),
    SocieteGeneraleParser(),
    BREDParser(),
    CreditMutuelParser(),
    AttijariwafaCodesParser(),
    BmceParser(),
    SahamParser(),
    CfgBankParser(),
    MarocColonnesParser(),
    # Ajouter ici les futurs parsers dédiés au fur et à mesure des besoins.
    GenericParser(),  # toujours en dernier
]


def detecter_parser(chemin_pdf: str) -> BaseParser:
    """Lit les premières pages du PDF et retourne le parser approprié."""
    try:
        with pdfplumber.open(chemin_pdf) as pdf:
            # Lire max 2 pages pour la détection (rapide)
            pages_detection = pdf.pages[:2]
            texte = "\n".join(p.extract_text() or "" for p in pages_detection)
    except Exception:
        texte = ""

    for parser in PARSERS_DISPONIBLES:
        if parser.can_parse(texte):
            return parser

    # Ne devrait jamais arriver car GenericParser accepte tout
    return GenericParser()

