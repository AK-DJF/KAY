# parsers/ocr_utils.py
# Repli OCR pour les relevés bancaires scannés (sans couche texte native) — même moteur
# (RapidOCR, local, gratuit) et même principe que services/facture_extractor.py pour les
# factures scannées : chaque page sans texte extractible est rasterisée en image, puis
# passée à l'OCR ; chaque bloc de texte détecté est converti au même format que
# pdfplumber.extract_words() (text/x0/x1/top), réutilisable tel quel par
# regrouper_lignes_par_position() et par les parseurs dédiés.

from typing import Optional

import numpy as np
from PIL import Image

_moteur_ocr = None


def _moteur_ocr_partage():
    """Charge RapidOCR une seule fois (les modèles ONNX restent en mémoire d'un appel
    à l'autre) — le premier appel est plus lent le temps du chargement."""
    global _moteur_ocr
    if _moteur_ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        _moteur_ocr = RapidOCR()
    return _moteur_ocr


def page_sans_texte(page) -> bool:
    """Vrai si cette page pdfplumber n'a aucune couche texte extractible (PDF scanné)."""
    return not (page.extract_text() or "").strip()


def ocr_mots_page(page, resolution: int = 200) -> list[dict]:
    """Rasterise une page pdfplumber sans texte natif et retourne ses mots reconnus par
    OCR, au format [{"text", "x0", "x1", "top"}, ...] — compatible avec
    regrouper_lignes_par_position() et avec le code des parseurs qui lisent des mots
    positionnés (extract_words)."""
    image = page.to_image(resolution=resolution).original
    resultat, _ = _moteur_ocr_partage()(np.asarray(image))
    if not resultat:
        return []

    mots = []
    for boite, texte, _score in resultat:
        xs = [pt[0] for pt in boite]
        ys = [pt[1] for pt in boite]
        mots.append({"text": texte, "x0": min(xs), "x1": max(xs), "top": min(ys)})
    return mots


def ocr_texte_page(page, resolution: int = 200) -> str:
    """Texte OCR d'une page, une ligne par ligne visuelle — utilisé pour la détection de
    banque (can_parse) quand la page n'a pas de couche texte native."""
    from .base import regrouper_lignes_par_position
    mots = ocr_mots_page(page, resolution=resolution)
    lignes = regrouper_lignes_par_position(mots)
    return "\n".join(" ".join(w["text"] for w in ligne) for ligne in lignes)


# Écart horizontal minimum, en points, pour considérer que deux positions de montant
# appartiennent à des colonnes Débit/Crédit réellement différentes plutôt qu'à la même
# colonne (petites variations d'alignement du texte/de l'OCR d'une ligne à l'autre) —
# calibré sur les relevés réels traités (Attijariwafa, BMCE, CIH, Crédit du Maroc, Banque
# Populaire) où le véritable écart Débit/Crédit dépasse toujours largement ce seuil.
ECART_MINIMUM_COLONNE = 40.0


def seuil_debit_credit(positions_x: list[float]) -> Optional[float]:
    """Détermine, à partir des positions horizontales (x0 ou x1) de tous les montants
    d'un document, le seuil séparant la colonne Débit de la colonne Crédit : le plus grand
    écart entre deux positions consécutives (triées), s'il est assez large pour être un
    vrai changement de colonne — sinon None (toutes les lignes appartiennent à la même
    colonne, ex. relevé ne comportant que des débits sur toute sa durée)."""
    positions = sorted(set(positions_x))
    if len(positions) < 2:
        return None
    ecarts = [(positions[i + 1] - positions[i], (positions[i] + positions[i + 1]) / 2)
              for i in range(len(positions) - 1)]
    ecart_max, seuil = max(ecarts)
    if ecart_max < ECART_MINIMUM_COLONNE:
        return None
    return seuil
