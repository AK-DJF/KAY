#!/usr/bin/env python3
"""Rapprochement factures encaissées <-> ventes.

Usage:
  python3 rapprochement.py VENTES.xlsx LISTE.txt [-o resultat.xlsx] [--feuille Sheet1]

VENTES.xlsx : export des ventes (colonnes: Numéro, Date de facturation, Origine,
              Partenaire, Total signé, État du paiement, État). L'en-tête est
              détecté automatiquement (ligne contenant "Numéro").
LISTE.txt   : un numéro de facture par ligne (ou .xlsx/.csv : 1re colonne).
Sortie      : Excel avec 4 feuilles : Trouvées, Absentes, Doublons, Résumé.
"""
import argparse, re, sys
from collections import Counter
import openpyxl
from openpyxl.styles import Font, PatternFill

def norm(s):
    return re.sub(r"\s+", "", str(s)).upper()

def suspect(k):
    m = re.match(r"^R?FAC/(\d+)/(\d+)$", k)
    if not m: return "Format inhabituel"
    if m[1] == "5202": return "Année probablement inversée (2025)"
    if int(m[1]) > 2025: return f"Année {m[1]}, hors de ce fichier"
    if len(m[2]) > 5: return "Trop de chiffres"
    return ""

def lire_liste(path):
    if path.lower().endswith((".xlsx", ".xlsm")):
        ws = openpyxl.load_workbook(path, data_only=True).active
        vals = [r[0] for r in ws.iter_rows(values_only=True)]
    else:
        vals = open(path, encoding="utf-8").read().splitlines()
    return [norm(v) for v in vals if v and re.search(r"FAC", str(v).upper())]

def lire_ventes(path, feuille):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[feuille] if feuille else wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    h = next(i for i, r in enumerate(rows) if r and r[0] and "num" in str(r[0]).lower())
    return {norm(r[0]): r for r in rows[h + 1:] if r and r[0]}

def style(ws, widths):
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor="305496")
    for i, w in enumerate(widths):
        ws.column_dimensions[chr(65 + i)].width = w
    ws.freeze_panes = "A2"

def main():
    a = argparse.ArgumentParser()
    a.add_argument("ventes"); a.add_argument("liste")
    a.add_argument("-o", "--out", default="resultat_rapprochement.xlsx")
    a.add_argument("--feuille")
    a = a.parse_args()
    V, L = lire_ventes(a.ventes, a.feuille), lire_liste(a.liste)
    cnt = Counter(L)
    uniques = list(dict.fromkeys(L))
    out = openpyxl.Workbook()

    ws = out.active; ws.title = "Trouvées"
    ws.append(["Numéro", "Date", "Partenaire", "Total signé", "État paiement", "Nb dans liste"])
    tot = 0
    for k in uniques:
        if k in V:
            r = V[k]; tot += r[4] or 0
            ws.append([r[0], r[1], r[3], r[4], r[5], cnt[k]])
    for r in ws.iter_rows(min_row=2):
        r[1].number_format = "dd/mm/yyyy"; r[3].number_format = "#,##0.00"
    n = ws.max_row
    ws.append(["TOTAL", "", "", f"=SUM(D2:D{n})"]); ws[n + 1][0].font = ws[n + 1][3].font = Font(bold=True)
    ws[n + 1][3].number_format = "#,##0.00"
    style(ws, [22, 14, 40, 15, 20, 14])

    ws = out.create_sheet("Absentes")
    ws.append(["Numéro", "Remarque", "Variante existante dans ventes ?", "Nb dans liste"])
    for k in uniques:
        if k not in V:
            alt = k[1:] if k.startswith("R") else "R" + k
            ws.append([k, suspect(k), alt if alt in V else "non", cnt[k]])
    style(ws, [24, 42, 32, 14])

    ws = out.create_sheet("Doublons")
    ws.append(["Numéro", "Nb dans liste"])
    for k, v in cnt.items():
        if v > 1: ws.append([k, v])
    style(ws, [24, 14])

    ws = out.create_sheet("Résumé")
    absentes = [k for k in uniques if k not in V]
    for l in [("Lignes dans la liste", len(L)), ("Numéros uniques", len(uniques)),
              ("Trouvés dans les ventes", len(uniques) - len(absentes)),
              ("Absents", len(absentes)), ("Doublons", sum(1 for v in cnt.values() if v > 1)),
              ("Total signé des trouvées", round(tot, 2))]:
        ws.append(l)
    ws.column_dimensions["A"].width = 30; ws.column_dimensions["B"].width = 16
    out.save(a.out)
    print(f"{len(uniques)} uniques | {len(uniques)-len(absentes)} trouvées | {len(absentes)} absentes -> {a.out}")
    if absentes: print("Absentes:", ", ".join(absentes))

if __name__ == "__main__":
    main()
