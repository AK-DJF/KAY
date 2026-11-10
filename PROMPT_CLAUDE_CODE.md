# Prompt à donner à Claude Code

Colle ceci dans Claude Code, avec tes deux fichiers dans le dossier :

---
J'ai un export Excel des ventes (`ventes.xlsx`, colonnes : Numéro, Date de facturation,
Origine, Partenaire, Total signé, État du paiement, État ; l'en-tête n'est pas forcément
en ligne 1) et une liste de numéros de factures encaissées (`liste.txt`, un numéro par ligne,
avec des doublons possibles et des fautes de frappe du type FAC/5202/...).

Utilise `rapprochement.py` (ou recrée-le s'il n'existe pas) pour :
1. Vérifier quelles factures de la liste existent dans les ventes (comparaison exacte du
   numéro, en gardant la différence FAC / RFAC, qui sont des factures et des avoirs).
2. Produire un Excel avec 4 feuilles : Trouvées (numéro, date, partenaire, total signé,
   état de paiement, nb d'occurrences), Absentes (avec suggestion de variante FAC/RFAC),
   Doublons, Résumé.
3. Me signaler les numéros suspects (année inversée, trop de chiffres, année 2026) et les
   factures trouvées mais non soldées (Non payées / Partiellement réglé).
Ne modifie pas mes fichiers sources. Montre-moi le résumé, puis commit et push le résultat.
---

Commande directe, sans Claude :

    python3 rapprochement.py ventes.xlsx liste.txt -o resultat.xlsx
