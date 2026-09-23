# parsers/util_position.py
# Utilitaires partagés pour parser les relevés bancaires marocains dont la
# mise en page ("Débit" à gauche, "Crédit" à droite) est perdue par
# l'extraction de texte linéaire de pdfplumber. On travaille donc au niveau
# des mots positionnés (x0/x1/top) pour reconstruire les colonnes.

import re
from typing import Optional

PATTERN_GROUPE_MILLIER = re.compile(r'^\d{1,3}$')
# Le dernier mot d'un montant : soit un simple "93,50" (< 1000, pas de séparateur de
# milliers), soit un montant à 4+ chiffres avec le point comme séparateur de milliers dans
# le même mot (ex: "1.100,00", "12.345,00" — format observé sur les relevés Saham Bank).
# Le cas où le séparateur de milliers est un espace (mot séparé, ex: "70 121,92") est géré
# à part par PATTERN_GROUPE_MILLIER + la fusion de mots dans extraire_montant_final.
PATTERN_GROUPE_FINAL = re.compile(r'^\d{1,3}(?:\.\d{3})*,\d{2}$')

# Écart maximal (en points) entre deux groupes de chiffres du même nombre
# (espace fine milliers). Un écart plus grand signifie un changement de
# colonne (ex: date valeur juste avant le montant) et doit stopper la fusion.
ECART_MAX_MEME_NOMBRE = 12


TOLERANCE_MEME_LIGNE = 3.0  # écart vertical (pt) toléré entre mots d'une même ligne


def grouper_lignes(page) -> dict[float, list[dict]]:
    """
    Regroupe les mots d'une page par ligne visuelle, triés de gauche à droite.
    Un simple `round(top)` casse parfois une même ligne en deux groupes quand
    des polices différentes (ex: en-têtes vs montants) ont des lignes de base
    décalées de moins d'un point — on regroupe donc par proximité verticale.
    """
    mots = sorted(page.extract_words(), key=lambda m: m['top'])
    lignes: dict[float, list[dict]] = {}
    cle_courante = None

    for m in mots:
        if cle_courante is None or (m['top'] - cle_courante) > TOLERANCE_MEME_LIGNE:
            cle_courante = m['top']
        lignes.setdefault(cle_courante, []).append(m)

    for cle in lignes:
        lignes[cle].sort(key=lambda m: m['x0'])
    return lignes


def extraire_montant_final(mots: list[dict]):
    """
    Cherche en partant de la fin de la ligne un montant (ex: '4 000 000,00'
    éclaté en plusieurs mots à cause des espaces milliers).
    Retourne (valeur, x1_droit, index_du_premier_mot_du_montant) ou None.
    """
    if not mots:
        return None

    fin = mots[-1]
    if not PATTERN_GROUPE_FINAL.match(fin['text']):
        return None

    i = len(mots) - 1
    x1 = fin['x1']
    parties = [fin['text']]
    borne_gauche = fin['x0']
    i -= 1
    while i >= 0 and PATTERN_GROUPE_MILLIER.match(mots[i]['text']) and (borne_gauche - mots[i]['x1']) <= ECART_MAX_MEME_NOMBRE:
        parties.insert(0, mots[i]['text'])
        borne_gauche = mots[i]['x0']
        i -= 1

    texte = ''.join(parties)
    try:
        # Retire les points milliers avant de convertir la virgule décimale (ex: "1.100,00").
        valeur = float(texte.replace('.', '').replace(',', '.'))
    except ValueError:
        return None

    return valeur, x1, i + 1


def seuil_colonnes(lignes: dict) -> float | None:
    """
    Calcule le seuil x1 séparant la colonne Débit (gauche) de la colonne
    Crédit (droite) à partir de tous les montants détectés sur la page.
    Retourne None si un seul groupe est détecté (page à une seule colonne).
    """
    x1s = []
    for mots in lignes.values():
        r = extraire_montant_final(mots)
        if r:
            x1s.append(r[1])

    if len(x1s) < 2:
        return None

    x1s.sort()
    ecarts = [(x1s[i + 1] - x1s[i], i) for i in range(len(x1s) - 1)]
    plus_grand_ecart, idx = max(ecarts, key=lambda t: t[0])

    # Si toutes les valeurs sont proches (pas de vraie séparation en 2 colonnes)
    if plus_grand_ecart < 15:
        return None

    return (x1s[idx] + x1s[idx + 1]) / 2


def est_credit(x1_montant: float, seuil: Optional[float]) -> Optional[bool]:
    """True = crédit (colonne de droite), False = débit, None = indéterminé."""
    if seuil is None:
        return None
    return x1_montant > seuil
