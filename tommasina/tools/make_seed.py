"""Extrait les lots du tableau Excel d'origine vers seed.json (données initiales)."""
import json, sys
from pathlib import Path
import openpyxl

src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent.parent / "Quote_part_terrain_TOMMASINA_2025.xlsx"
ws = openpyxl.load_workbook(src, data_only=True)["Proprieté_Tommassina"]

def num(v):
    return v if isinstance(v, (int, float)) else None

def txt(v):
    return str(v).strip() if v is not None and str(v).strip() else None

lots = []
for r in range(6, 86):
    g = lambda c: ws.cell(r, c).value
    d = g(1)
    k = num(g(11))
    lots.append({
        "date_contrat": d.date().isoformat() if hasattr(d, "date") else None,
        "client": txt(g(2)), "tf": txt(g(3)), "propriete": txt(g(4)),
        "indice": txt(g(5)), "niveau": txt(g(6)), "consistance": txt(g(7)),
        "surface_titre": num(g(8)), "surface_totale": num(g(9)),
        "prix_m2": k if k not in (None, 18515.25) else None,
        "prix_ttc": num(g(17)), "encaisse": num(g(18)), "notes": None,
    })
json.dump({"parametres": {"surface_terrain": 692, "prix_terrain_total": 12110000 + 702552.5,
                          "prix_m2": 18515.25, "tva": 0.2},
           "lots": lots}, open(Path(__file__).parent.parent / "seed.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
print(len(lots), "lots")
