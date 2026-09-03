# diagnostic_cih.py
# Script de diagnostic autonome — à lancer DEPUIS le dossier app (là où se trouve app.py)
# avec la même commande Python que le serveur, ex. :
#
#   python diagnostic_cih.py "chemin\vers\inte_07__releve2026.pdf"
#
# Objectif : comprendre pourquoi l'import de ce fichier échoue sur cette machine alors
# qu'il fonctionne dans l'environnement de test. N'écrit rien en base, ne modifie rien —
# lecture seule. Affiche tout ce qu'il se passe, y compris les erreurs complètes.

import sys
import os
import traceback

print("=" * 70)
print("DIAGNOSTIC KWIKA — extraction relevé")
print("=" * 70)

print(f"\nPython      : {sys.version}")
print(f"Exécutable  : {sys.executable}")
print(f"Dossier     : {os.getcwd()}")

try:
    import pdfplumber
    print(f"pdfplumber  : {pdfplumber.__version__}")
except Exception as e:
    print(f"pdfplumber  : ERREUR À L'IMPORT — {e}")

try:
    import pypdfium2
    print(f"pypdfium2   : {pypdfium2.V_PDFIUM if hasattr(pypdfium2, 'V_PDFIUM') else getattr(pypdfium2, '__version__', '?')}")
except Exception as e:
    print(f"pypdfium2   : non détectable directement ({e})")

try:
    from dotenv import load_dotenv
    load_dotenv()
    print(".env chargé : oui")
except Exception as e:
    print(f".env chargé : ERREUR — {e}")

cle = os.environ.get("OPENROUTER_API_KEY", "").strip()
if cle:
    print(f"OPENROUTER_API_KEY : présente (longueur={len(cle)}, commence par '{cle[:6]}...')")
else:
    print("OPENROUTER_API_KEY : ABSENTE ou vide — le chemin IA ne sera PAS tenté, tout repose sur les parseurs locaux")

if len(sys.argv) < 2:
    print("\nUsage : python diagnostic_cih.py \"chemin\\vers\\le_fichier.pdf\"")
    sys.exit(1)

chemin_pdf = sys.argv[1]
print(f"\nFichier testé : {chemin_pdf}")
print(f"Existe        : {os.path.exists(chemin_pdf)}")
if not os.path.exists(chemin_pdf):
    print("=> Le fichier n'existe pas à ce chemin, corrigez le chemin et relancez.")
    sys.exit(1)

# ── Étape 1 : lecture brute pdfplumber (indépendante du code de l'appli) ──
print("\n" + "-" * 70)
print("ÉTAPE 1 : ouverture brute avec pdfplumber")
print("-" * 70)
try:
    import pdfplumber
    with pdfplumber.open(chemin_pdf) as pdf:
        print(f"Nombre de pages : {len(pdf.pages)}")
        for i, page in enumerate(pdf.pages):
            texte = page.extract_text() or ""
            tables = page.extract_tables()
            print(f"  Page {i+1}: {len(texte)} caractères de texte natif, {len(tables)} table(s) détectée(s)")
            if i == 0:
                print(f"  Extrait du texte page 1 (200 premiers caractères) :")
                print("  " + repr(texte[:200]))
except Exception:
    print("ERREUR pendant la lecture brute pdfplumber :")
    traceback.print_exc()

# ── Étape 2 : détection de banque (code réel de l'appli) ──
print("\n" + "-" * 70)
print("ÉTAPE 2 : détection du parseur (parsers.detector.detecter_parser)")
print("-" * 70)
try:
    from parsers.detector import detecter_parser
    parser = detecter_parser(chemin_pdf)
    print(f"Parseur détecté : {parser.NOM_BANQUE} ({type(parser).__name__})")
except Exception:
    print("ERREUR pendant la détection :")
    traceback.print_exc()
    parser = None

# ── Étape 3 : extraction des transactions avec ce parseur ──
if parser is not None:
    print("\n" + "-" * 70)
    print("ÉTAPE 3 : extraction avec le parseur détecté (parser.parse)")
    print("-" * 70)
    try:
        transactions = parser.parse(chemin_pdf)
        total_debit = sum(t.debit or 0 for t in transactions)
        total_credit = sum(t.credit or 0 for t in transactions)
        print(f"Transactions extraites : {len(transactions)}")
        print(f"Total débit  : {total_debit}")
        print(f"Total crédit : {total_credit}")
        if transactions:
            print("Premières transactions :")
            for t in transactions[:3]:
                print(f"  - {t.date} | {t.libelle[:50]!r} | débit={t.debit} crédit={t.credit}")
    except Exception:
        print("ERREUR pendant parser.parse() :")
        traceback.print_exc()

# ── Étape 4 : le chemin complet réellement utilisé par l'appli (services.extractor) ──
print("\n" + "-" * 70)
print("ÉTAPE 4 : chemin complet utilisé par l'appli (services.extractor.extraire_transactions)")
print("-" * 70)
try:
    from services.extractor import extraire_transactions
    transactions, banque = extraire_transactions(chemin_pdf)
    print(f"Banque détectée : {banque}")
    print(f"Transactions extraites : {len(transactions)}")
except Exception:
    print("ERREUR pendant extraire_transactions() — C'EST TRÈS PROBABLEMENT LA CAUSE DU 422 :")
    traceback.print_exc()

print("\n" + "=" * 70)
print("FIN DU DIAGNOSTIC — copiez-collez TOUT ce qui précède dans le chat")
print("=" * 70)
