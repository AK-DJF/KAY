# parsers/maroc.py
# Parsers pour les banques marocaines sans exemple de relevé réel pour calibrer un
# repérage de colonnes dédié — voir attijariwafa.py/bmce.py/saham.py/cih.py/
# credit_du_maroc.py/banque_populaire.py pour les banques calibrées sur de vrais relevés.
# En attendant un exemple, ces classes réutilisent l'extraction générique par
# tableaux/regex de GenericParser (déjà compatible avec les dates JJ/MM/AAAA et le format
# à virgule décimale utilisés au Maroc) et n'ajoutent que la détection + le libellé de
# banque corrects.

from .generic import GenericParser


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
