# parsers/detector.py
# Détecte automatiquement la banque et retourne le bon parser

import pdfplumber
from .base import BaseParser
from .qonto import QontoParser
from .revolut import RevolutParser
from .societe_generale import SocieteGeneraleParser
from .bred import BREDParser
from .credit_mutuel import CreditMutuelParser
from .attijariwafa import AttijariwafaBankParser
from .bmce import BMCEBankOfAfricaParser
from .saham import SahamBankParser
from .maroc import (
    BanquePopulaireMarocParser, CIHBankParser, SocieteGeneraleMarocParser, CreditDuMarocParser,
)
from .generic import GenericParser

# Liste ordonnée des parsers spécifiques (du plus précis au plus générique).
# Qonto, Revolut, Société Générale, BRED, Crédit Mutuel, Attijariwafa Bank, BMCE Bank of
# Africa et Saham Bank sont calibrés sur des relevés réels. Les autres parsers marocains
# (voir maroc.py) détectent correctement la banque mais réutilisent l'extraction générique
# faute d'exemple de relevé réel pour calibrer un repérage dédié.
PARSERS_DISPONIBLES: list[BaseParser] = [
    QontoParser(),
    RevolutParser(),
    SocieteGeneraleParser(),
    BREDParser(),
    CreditMutuelParser(),
    AttijariwafaBankParser(),
    BMCEBankOfAfricaParser(),
    SahamBankParser(),
    BanquePopulaireMarocParser(),
    CIHBankParser(),
    SocieteGeneraleMarocParser(),
    CreditDuMarocParser(),
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

