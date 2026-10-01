# services/import_tableur.py
# Import des mouvements d'un relevé depuis un fichier Excel (.xlsx) ou CSV — solution de
# secours quand ni les parseurs locaux ni l'IA ne lisent correctement un PDF (relevé scanné de
# mauvaise qualité, mise en page décalée…), ou quand la banque fournit déjà un export tableur.
# Colonnes attendues (en-tête, ordre libre, accents/majuscules indifférents) :
#   Date | Libellé | Débit | Crédit

import csv
import io
import unicodedata
from datetime import date, datetime
from pathlib import Path

from parsers.base import Transaction
from services.extractor import _date, _montant

EXTENSIONS_TABLEUR = (".xlsx", ".csv")


def _normaliser(texte) -> str:
    texte = unicodedata.normalize("NFKD", str(texte or "")).encode("ascii", "ignore").decode()
    return texte.strip().lower()


def _lignes_xlsx(contenu: bytes) -> list[list]:
    import openpyxl
    classeur = openpyxl.load_workbook(io.BytesIO(contenu), data_only=True, read_only=True)
    return [list(ligne) for ligne in classeur.worksheets[0].iter_rows(values_only=True)]


def _lignes_csv(contenu: bytes) -> list[list]:
    for encodage in ("utf-8-sig", "cp1252"):
        try:
            texte = contenu.decode(encodage)
            break
        except UnicodeDecodeError:
            continue
    separateur = ";" if texte.count(";") >= texte.count(",") else ","
    return [ligne for ligne in csv.reader(io.StringIO(texte), delimiter=separateur)]


def _colonnes(entete: list) -> dict | None:
    noms = [_normaliser(c) for c in entete]
    trouver = lambda *cles: next((i for i, n in enumerate(noms) if any(n.startswith(c) for c in cles)), None)
    colonnes = {
        "date": trouver("date"),
        "libelle": trouver("libelle", "description", "operation"),
        "debit": trouver("debit"),
        "credit": trouver("credit"),
    }
    if colonnes["date"] is None or colonnes["debit"] is None or colonnes["credit"] is None:
        return None
    return colonnes


def extraire_transactions_tableur(nom_fichier: str, contenu: bytes) -> list[Transaction]:
    extension = Path(nom_fichier).suffix.lower()
    lignes = _lignes_xlsx(contenu) if extension == ".xlsx" else _lignes_csv(contenu)

    colonnes = None
    for index, ligne in enumerate(lignes):
        colonnes = _colonnes(ligne)
        if colonnes:
            lignes = lignes[index + 1:]
            break
    if not colonnes:
        raise ValueError("En-tête introuvable : le fichier doit contenir les colonnes Date, Libellé, Débit et Crédit")

    transactions = []
    for ligne in lignes:
        valeur = lambda cle: ligne[colonnes[cle]] if colonnes[cle] is not None and colonnes[cle] < len(ligne) else None
        brute = valeur("date")
        tx_date = brute.date() if isinstance(brute, datetime) else brute if isinstance(brute, date) else _date(brute)
        debit, credit = _montant(valeur("debit")), _montant(valeur("credit"))
        if tx_date is None or (debit is None and credit is None):
            continue
        transactions.append(Transaction(
            date=tx_date,
            libelle=str(valeur("libelle") or "").strip(),
            debit=debit,
            credit=credit,
            solde=None,
            banque="Import tableur",
            fichier_source=nom_fichier,
        ))
    return transactions
