# Mémoire du projet — Portail Comptablio

> Ce fichier sert de mémoire vivante pour l'appli Portail Comptablio.
> Il doit être mis à jour à la fin de chaque session de travail (nouvelles décisions, avancement, points en attente).

## Résumé du projet

Point d'entrée unique local pour la marque **Comptablio** : une page d'accueil
qui liste les applications du dossier `comptablio/` (KAY PV AGO, KAY JURIDIQUE,
Kikou Numérisation) et ouvre chacune dans son propre onglet. Ne remplace pas
les applications elles-mêmes — chacune continue de tourner sur son propre
serveur/port, avec son propre login.

## État d'avancement

- Appli Flask minimale, sans base de données : `app.py` (liste Python des 3
  applications) + `templates/index.html` + `static/css/style.css`. Port 8090.
- Bandeau de navigation partagé (`comptablio-nav`) créé ici comme copie
  maître (`static/partials/comptablio-nav.html` + `static/css/
  comptablio-nav.css`) puis dupliqué à l'identique dans les 3 applications
  ciblées (pas de build partagé entre les apps du dossier, cf. `CLAUDE.md` de
  `K.COMPTA`) — changement additif uniquement dans chaque appli, aucune route
  ni authentification modifiée.
- Lanceur unique `comptablio/Démarrer Portail Comptablio.bat` (à la racine du
  dossier `comptablio/`) : démarre le portail + les 3 applications chacune
  dans sa propre fenêtre, puis ouvre `http://127.0.0.1:8090`. Kikou
  Numérisation est lancée explicitement sur le port 8003 (le défaut dans son
  code est 8000). Les `.bat` individuels de chaque appli restent inchangés et
  utilisables séparément.

## Décisions prises

- Pas de reverse-proxy / origine unique : KAY PV AGO et Kikou Numérisation ont
  des appels `fetch()` codés en dur en chemin absolu (`/api/...`), qui
  casseraient sous un préfixe de chemin partagé sans réécrire leur JS. Le
  portail reste donc une page de liens (chaque appli garde son port), pas un
  proxy.
- Pas de compte unifié en v1 (décision explicite de l'utilisateur) : chaque
  application garde son propre système de login. Une unification des comptes
  (SSO) est envisageable en v2 si besoin.
- Déploiement public (nom de domaine, hébergement) explicitement repoussé —
  cette v1 est strictement locale (`127.0.0.1`).
- Périmètre v1 limité à 3 applications (choix explicite de l'utilisateur) :
  l'appli Comptablio (compta cabinet) et les copies obsolètes (ancien
  `numerisation-app`, backup Kikou TVA) ne sont pas incluses.

## Points en attente / prochaines étapes

- Pas de vérification en direct de l'état (en ligne/hors ligne) des 3
  applications sur la page d'accueil — juste des liens statiques. Pourrait
  être ajouté plus tard (ping cross-origin) si utile.
- v2 possible : compte unique partagé entre les applications, puis
  éventuellement un vrai déploiement public une fois un nom de
  domaine/hébergement choisi.

## Journal des sessions

### 2026-09-22
- Création du Portail Comptablio (page d'accueil, port 8090) et ajout du
  bandeau de navigation partagé dans KAY PV AGO, KAY JURIDIQUE et Kikou
  Numérisation, suite à la demande de l'utilisateur de regrouper les
  applications sur un seul site. V1 strictement locale, périmètre limité à
  ces 3 applications, logins gardés séparés (décisions confirmées par
  l'utilisateur avant implémentation).

### 2026-10-11
- Ajout de **E-COMPTA** (port 5400, dossier `comptablio/E-COMPTA`) : carte sur la
  page d'accueil (`app.py`), lien dans le bandeau partagé (copie maître + copies
  de KAY PV AGO, KAY JURIDIQUE, Kikou Numérisation et Fatoura Pro, ajout d'une
  ligne seulement), démarrage dans les deux lanceurs (portail + 5 applications).
  Le bandeau est aussi affiché dans E-COMPTA, sauf dans l'espace client.
- Nouveau look repris de l'export Chantan « Comptablio Portail Expert »
  (`my-chantan-app-comptablio-main.zip`, React) : seul le design a été
  recopié dans la page Flask (pas de React, rien à installer). Nouveau
  `templates/index.html` + `static/css/portail.css` ; photos « Sans / Avec
  Comptablio » téléchargées depuis le CDN Chantan dans `static/img/*.webp`.
  Les boutons « Ouvrir » mènent bien à chaque appli (liste `APPLICATIONS` de
  `app.py`, E-COMPTA incluse). Ancienne page gardée :
  `templates/index-ancien-2026-10-11.html` + `static/css/style.css`.
- Le look Chantan n'a pas plu : remplacé le même jour par un style « logiciel
  SaaS » (fond clair bleuté, cartes blanches, boutons dégradé bleu-violet,
  icônes colorées, polices Inter / Plus Jakarta Sans). Seul
  `static/css/portail.css` (et la ligne des polices dans `index.html`) a
  changé, contenu et liens identiques. Look Chantan gardé dans
  `static/css/portail-chantan.css`.
- Puis remplacé par un style « comptable » (demande de l'utilisateur) : vert
  comptable + marine, fond papier quadrillé, marge rouge de registre, doubles
  filets sous les titres, polices IBM Plex (Sans/Serif/Mono). Style SaaS
  gardé dans `static/css/portail-saas.css`.
- Style comptable refusé (« pas pro »). Refonte « pro » façon logiciels de
  gestion (Pennylane/Qonto) : blanc + bleu marine, accent bleu unique, police
  Inter ; titre à gauche + photos en montage à droite ; **applications placées
  juste sous le titre** en liste (ligne entière cliquable + bouton Ouvrir) pour
  un accès rapide. Ordre des sections changé dans `templates/index.html`
  (ancien gabarit : `templates/index-avant-pro.html`, style comptable :
  `static/css/portail-comptable.css`).
- Photos remplacées à la demande (jeunes, fille avec frange et cheveux longs) :
  `static/img/equipe-avec.jpg` (Unsplash photo-1732685934798) et
  `static/img/jeune-sans.jpg` (Unsplash photo-1758874572368), licence
  Unsplash (usage libre). Anciennes photos Chantan gardées (`*.webp`).
- Photo « Avec » remplacée par une fille + plusieurs garçons (demande) :
  `static/img/equipe-groupe-avec.jpg` (Unsplash photo-1520881363902, café,
  3 jeunes). Cadre photo élargi (paysage) pour ne couper personne.
  `equipe-avec.jpg` (fille à frange + 1 garçon) gardée en réserve.
