# parsers/maroc.py
# Parsers pour les banques marocaines — pas de relevé réel disponible pour calibrer un
# repérage de colonnes dédié (comme pour BRED/Société Générale/Crédit Mutuel), donc ces
# classes réutilisent l'extraction générique par tableaux/regex de GenericParser (déjà
# compatible avec les dates DD/MM/YYYY et le format à virgule décimale utilisés au Maroc)
# et n'ajoutent que la détection + le libellé de banque corrects. À affiner avec un vrai
# relevé PDF de chaque banque dès qu'un exemple est disponible.

from .generic import GenericParser


class AttijariwafaBankParser(GenericParser):
    NOM_BANQUE = "Attijariwafa Bank"

    def can_parse(self, texte_complet: str) -> bool:
        return "ATTIJARIWAFA" in texte_complet.upper()


class BanquePopulaireMarocParser(GenericParser):
    NOM_BANQUE = "Banque Populaire"

    def can_parse(self, texte_complet: str) -> bool:
        texte = texte_complet.upper()
        # "BANQUE POPULAIRE" seul est ambigu avec le réseau français (BRED en fait partie) —
        # exigé avec "MAROC", ou les formes propres au groupe marocain (BCP).
        if "BANQUE CENTRALE POPULAIRE" in texte or "GROUPE BCP" in texte:
            return True
        return "BANQUE POPULAIRE" in texte and "MAROC" in texte


class BMCEBankOfAfricaParser(GenericParser):
    NOM_BANQUE = "BMCE Bank of Africa"

    def can_parse(self, texte_complet: str) -> bool:
        texte = texte_complet.upper()
        return "BMCE" in texte or "BANK OF AFRICA" in texte


class CIHBankParser(GenericParser):
    NOM_BANQUE = "CIH Bank"

    def can_parse(self, texte_complet: str) -> bool:
        texte = texte_complet.upper()
        return "CIH BANK" in texte or ("CIH" in texte and "MAROC" in texte)


class SocieteGeneraleMarocParser(GenericParser):
    NOM_BANQUE = "Société Générale Maroc"

    def can_parse(self, texte_complet: str) -> bool:
        texte = texte_complet.upper()
        return ("SOCIETE GENERALE" in texte or "SOCIÉTÉ GÉNÉRALE" in texte) and "MAROC" in texte


class CreditDuMarocParser(GenericParser):
    NOM_BANQUE = "Crédit du Maroc"

    def can_parse(self, texte_complet: str) -> bool:
        texte = texte_complet.upper()
        return "CREDIT DU MAROC" in texte or "CRÉDIT DU MAROC" in texte
