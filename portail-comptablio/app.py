# app.py
# Portail Comptablio — page d'accueil unique regroupant les applications
# KAY PV AGO, KAY JURIDIQUE, Kikou Numérisation, Fatoura Pro et E-COMPTA, chacune restant sur son
# propre serveur/port (pas de reverse-proxy, cf. memoire.md).
# Lancer avec : python app.py

from flask import Flask, render_template

app = Flask(__name__)

APPLICATIONS = [
    {
        "nom": "KAY PV AGO",
        "icone": "PV",
        "description": "Procès-verbaux d'assemblée générale ordinaire (SARL, SARLAU, SA), fiche société permanente et archive multi-exercices.",
        "url": "http://127.0.0.1:5000",
    },
    {
        "nom": "KAY JURIDIQUE",
        "icone": "KJ",
        "description": "Génération de projets de documents juridiques et commerciaux conformes au droit marocain (statuts, PV, contrats, mise en demeure...).",
        "url": "http://127.0.0.1:8020",
    },
    {
        "nom": "Kikou Numérisation",
        "icone": "KN",
        "description": "Numérisation et extraction automatique des factures et relevés bancaires, avec rapprochement par société.",
        "url": "http://127.0.0.1:8003",
    },
    {
        "nom": "Fatoura Pro",
        "icone": "FP",
        "description": "Facturation et devis pour vos clients : lignes d'articles, calcul TVA, numérotation automatique, export PDF.",
        "url": "http://127.0.0.1:5100",
    },
    {
        "nom": "E-COMPTA",
        "icone": "EC",
        "description": "Gestion comptable multi-cabinets : relevés, factures, écritures, lettrage, TVA, liasse fiscale et espace de dépôt pour les clients.",
        "url": "http://127.0.0.1:5400",
    },
]


@app.get("/")
def index():
    return render_template("index.html", applications=APPLICATIONS)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8090, debug=False)
