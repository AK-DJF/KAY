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
from .cih import CIHBankParser
from .credit_du_maroc import CreditDuMarocParser
from .banque_populaire import BanquePopulaireMarocParser
from .maroc import SocieteGeneraleMarocParser
from .generic import GenericParser
from .ocr_utils import ocr_texte_page

# Liste ordonnée des parsers spécifiques (du plus précis au plus générique).
# Qonto, Revolut, Société Générale, BRED, Crédit Mutuel, Attijariwafa Bank, BMCE Bank of
# Africa, Saham Bank, CIH Bank, Banque Populaire et Crédit du Maroc sont calibrés sur des
# relevés réels (les 2 dernières via OCR — voir ocr_utils.py, relevés reçus scannés sans
# couche texte). Société Générale Maroc réutilise l'extraction générique (voir maroc.py)
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
    CIHBankParser(),
    BanquePopulaireMarocParser(),
    SocieteGeneraleMarocParser(),
    CreditDuMarocParser(),
    # Ajouter ici les futurs parsers dédiés au fur et à mesure des besoins.
    GenericParser(),  # toujours en dernier
]


def detecter_parser(chemin_pdf: str) -> BaseParser:
    """Lit les premières pages du PDF et retourne le parser approprié.

    Deux passes : d'abord le texte natif (rapide, cas normal). Si aucun parser dédié ne
    reconnaît la banque sur ce texte — PDF scanné sans couche texte, ou couche texte
    présente mais corrompue (police mal encodée, caractères mélangés/illisibles, vu sur un
    relevé Banque Populaire réel malgré un texte non vide) — on retente avec le texte OCR
    des mêmes pages avant de retomber sur le parseur générique."""
    try:
        with pdfplumber.open(chemin_pdf) as pdf:
            pages_detection = pdf.pages[:2]
            texte = "\n".join(p.extract_text() or "" for p in pages_detection)

            for parser in PARSERS_DISPONIBLES[:-1]:  # tous sauf GenericParser (dernier, accepte tout)
                if parser.can_parse(texte):
                    return parser

            texte_ocr = "\n".join(ocr_texte_page(p) for p in pages_detection)
    except Exception:
        return GenericParser()

    for parser in PARSERS_DISPONIBLES:
        if parser.can_parse(texte_ocr):
            return parser

    # Ne devrait jamais arriver car GenericParser accepte tout
    return GenericParser()

