# database.py
# Modèles SQLAlchemy + gestion de la session SQLite
# Schéma : users / societes / comptes_bancaires / releves / mouvements / factures / anomalies
# Voir prompt-cadrage_application-numerisation_2026-07-28.md section 2.1 pour le schéma prévisionnel.

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Column, Integer, String, Float, Date, DateTime, Boolean,
    ForeignKey, UniqueConstraint, create_engine, text,
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker, relationship, Session

DB_PATH = Path(__file__).parent / "data.db"
engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


# ── Accès / utilisateurs (section 7 du cadrage) ────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id              = Column(Integer, primary_key=True, index=True)
    email           = Column(String, unique=True, nullable=False, index=True)
    password_hash   = Column(String, nullable=False)
    role            = Column(String, default="admin", nullable=False)  # admin | collaborateur
    actif           = Column(Boolean, default=True, nullable=False)
    date_creation   = Column(DateTime, default=datetime.utcnow)


# ── Sociétés et comptes bancaires (section 8) ───────────────────────────────────

class Societe(Base):
    __tablename__ = "societes"

    id            = Column(Integer, primary_key=True, index=True)
    nom           = Column(String, nullable=False, unique=True)
    date_creation = Column(DateTime, default=datetime.utcnow)

    # Numérotation d'écriture FEC (évolution du 2026-08-16) : un compteur par société,
    # partagé entre le FEC des relevés et celui des factures — incrémenté à chaque écriture
    # générée (un mouvement ou une facture = une écriture = 1 à 3 lignes partageant ce numéro),
    # jamais remis à zéro entre deux exports.
    prochain_numero_ecriture = Column(Integer, default=1, nullable=False)

    # Codes journaux pour les écritures de factures (le module relevés a déjà son propre
    # journal_comptable par compte bancaire) — évolution du 2026-08-16.
    journal_achats = Column(String, default="ACH", nullable=False)
    journal_ventes = Column(String, default="VTE", nullable=False)

    # Validation de la société (évolution du 2026-08-20, demande Anis) : une société n'est
    # "validée" que lorsqu'elle a au moins un compte tiers Fournisseur, un Client et un
    # Personnel — cf. règle de numérotation des tiers dans app.py (POST /api/societes/{id}/valider).
    validee = Column(Boolean, default=False, nullable=False)

    comptes  = relationship("CompteBancaire", back_populates="societe", cascade="all, delete-orphan")
    factures = relationship("Facture", back_populates="societe", cascade="all, delete-orphan")


class CompteBancaire(Base):
    __tablename__ = "comptes_bancaires"

    id                      = Column(Integer, primary_key=True, index=True)
    societe_id              = Column(Integer, ForeignKey("societes.id"), nullable=False)
    banque                  = Column(String, nullable=False)
    libelle                 = Column(String, nullable=False)
    devise                  = Column(String, default="EUR", nullable=False)  # EUR | MAD
    mois_debut_exercice     = Column(Integer, default=1, nullable=False)      # 1-12, paramétrable (section 4.4)
    date_creation           = Column(DateTime, default=datetime.utcnow)

    # Champs comptables (évolution du 2026-08-03)
    numero_compte_bancaire  = Column(String, nullable=True)   # numéro du compte à la banque (IBAN ou interne)
    numero_compte_comptable = Column(String, nullable=True)   # compte du plan comptable rattaché (ex. 51220000)
    derniers_chiffres       = Column(String, nullable=True)   # 4 derniers chiffres du relevé, identification visuelle
    journal_comptable       = Column(String, nullable=True)   # code journal (ex. BQ1) — utilisé aussi comme libellé

    societe  = relationship("Societe", back_populates="comptes")
    releves  = relationship("Releve", back_populates="compte", cascade="all, delete-orphan")


# ── Module A : relevés bancaires (section 4) ─────────────────────────────────────

class Releve(Base):
    """Un relevé mensuel (un fichier PDF = un mois = une ligne)."""
    __tablename__ = "releves"
    __table_args__ = (
        UniqueConstraint("compte_id", "mois", "annee", name="uq_releve_compte_periode"),
    )

    id                     = Column(Integer, primary_key=True, index=True)
    compte_id              = Column(Integer, ForeignKey("comptes_bancaires.id"), nullable=False)
    mois                   = Column(Integer, nullable=False)   # 1-12
    annee                  = Column(Integer, nullable=False)
    nom_fichier            = Column(String, nullable=False)
    banque_detectee        = Column(String, default="Inconnu")
    date_import            = Column(DateTime, default=datetime.utcnow)

    solde_initial          = Column(Float, nullable=False)
    solde_final_attendu    = Column(Float, nullable=False)   # saisi manuellement par Anis (section 4.4)
    solde_final_calcule    = Column(Float, nullable=True)    # solde_initial + somme des mouvements extraits

    # ok = solde vérifié, ecart = alerte bloquante, en_attente = pas encore contrôlé
    statut                 = Column(String, default="en_attente", nullable=False)
    nb_mouvements          = Column(Integer, default=0)

    compte       = relationship("CompteBancaire", back_populates="releves")
    mouvements   = relationship("Mouvement", back_populates="releve", cascade="all, delete-orphan")


class Mouvement(Base):
    """Une ligne de transaction extraite d'un relevé."""
    __tablename__ = "mouvements"

    id             = Column(Integer, primary_key=True, index=True)
    releve_id      = Column(Integer, ForeignKey("releves.id"), nullable=False)
    date           = Column(Date, nullable=False)
    libelle        = Column(String, nullable=False)
    debit          = Column(Float, nullable=True)
    credit         = Column(Float, nullable=True)
    categorie      = Column(String, default="Non catégorisé")

    # Rattachement de la contrepartie pour la génération d'écriture (évolution du 2026-08-03)
    # tiers_id pointe vers la table tiers, qu'il s'agisse d'un tiers (client/fournisseur/salarié)
    # ou d'un compte comptable ordinaire (type "compte"). NULL -> repli sur le compte récurrent par défaut à l'export.
    tiers_id = Column(Integer, ForeignKey("tiers.id"), nullable=True)

    # Compte de TVA choisi manuellement pour ce mouvement (uniquement pertinent quand tiers_id pointe
    # vers un compte de type "compte") — optionnel, saisi ligne par ligne (évolution du 2026-08-03).
    taux_tva_id = Column(Integer, ForeignKey("taux_tva.id"), nullable=True)

    # Date du dernier export (Excel ou FEC) incluant ce mouvement — NULL si jamais exporté
    # (évolution du 2026-08-04, sélection + suivi d'export ligne par ligne).
    date_dernier_export = Column(DateTime, nullable=True)

    releve   = relationship("Releve", back_populates="mouvements")
    tiers    = relationship("Tiers")
    taux_tva = relationship("TauxTVA")


# ── Comptes tiers (clients/fournisseurs) et compte d'attente (évolution du 2026-08-03) ──

class Tiers(Base):
    """Compte tiers ou compte comptable ordinaire, importé depuis une liste Excel propre à chaque société,
    ou créé à la volée depuis Relevés/Factures/Sociétés & comptes (évolution du 2026-08-21).
    type = client | fournisseur | salarie | compte (compte ordinaire de charge/produit, sans tiers)."""
    __tablename__ = "tiers"
    __table_args__ = (
        UniqueConstraint("societe_id", "numero_compte", name="uq_tiers_societe_numero"),
    )

    id             = Column(Integer, primary_key=True, index=True)
    societe_id     = Column(Integer, ForeignKey("societes.id"), nullable=False)
    numero_compte  = Column(String, nullable=False)
    intitule       = Column(String, nullable=False)
    type           = Column(String, nullable=False)   # client | fournisseur | salarie | compte
    date_import    = Column(DateTime, default=datetime.utcnow)

    # Compte collectif de rattachement (401 Fournisseurs, 411 Clients, 420 Personnel...) — évolution du
    # 2026-08-21, demande Anis. Requis uniquement pour les comptes tiers (client/fournisseur/salarie),
    # jamais pour un compte général (type "compte") ; purement classificatoire, aucune contrainte de
    # préfixe numérique entre ce compte et le numéro du tiers créé dessous.
    compte_collectif_id = Column(Integer, ForeignKey("comptes_collectifs.id"), nullable=True)

    # Journal d'appartenance du compte — purement informatif (aucun impact sur la génération FEC,
    # qui continue à déterminer son JournalCode par le contexte : achats/ventes de la société ou
    # journal_comptable du compte bancaire). Évolution du 2026-08-21, demande Anis.
    journal = Column(String, nullable=True)

    societe          = relationship("Societe")
    compte_collectif = relationship("ComptesCollectifs")


class ParametresGlobaux(Base):
    """Ancien réglage unique (compte d'attente) — remplacé par ComptesRecurrents (évolution du 2026-08-03).
    Conservé uniquement pour la migration de la valeur existante ; plus utilisé par l'application."""
    __tablename__ = "parametres_globaux"

    id                      = Column(Integer, primary_key=True)
    compte_attente_numero   = Column(String, nullable=True)
    compte_attente_libelle  = Column(String, nullable=True)


class TauxTVA(Base):
    """Taux de TVA et compte comptable associé, propres à chaque société (évolution du 2026-08-16,
    demande Anis : remplace le catalogue global partagé + activation par société — chaque société
    définit directement ses propres taux, plus simple à comprendre, tout ce qui concerne une société
    est regroupé au même endroit dans l'onglet Sociétés & comptes). Utilisé pour scinder
    automatiquement un mouvement/une facture rattaché à un compte comptable ordinaire (charge/
    produit) entre part HT et part TVA à l'export FEC."""
    __tablename__ = "taux_tva"
    __table_args__ = (
        UniqueConstraint("societe_id", "taux", name="uq_taux_tva_societe_taux"),
    )

    id                 = Column(Integer, primary_key=True, index=True)
    # Nullable pour les lignes historiques créées avant cette évolution (migration douce) ;
    # toujours renseigné pour toute nouvelle ligne créée via l'API.
    societe_id         = Column(Integer, ForeignKey("societes.id"), nullable=True)
    taux               = Column(Float, nullable=False)   # ex. 20, 5.5, 5, 0
    numero_compte_tva  = Column(String, nullable=True)   # peut être vide tant que non complété
    intitule           = Column(String, nullable=True)
    date_creation      = Column(DateTime, default=datetime.utcnow)

    societe = relationship("Societe")


class ComptesRecurrents(Base):
    """Comptes fixes/récurrents (compte d'attente, frais bancaires, prélèvements...), propres à
    chaque société (évolution du 2026-08-16, demande Anis — remplace la liste globale partagée par
    toutes les sociétés). Un seul par société peut être marqué par défaut (est_defaut) : c'est le
    repli automatique à l'export FEC pour cette société."""
    __tablename__ = "comptes_recurrents"
    __table_args__ = (
        UniqueConstraint("societe_id", "numero_compte", name="uq_compte_recurrent_societe_numero"),
    )

    id             = Column(Integer, primary_key=True, index=True)
    societe_id     = Column(Integer, ForeignKey("societes.id"), nullable=True)  # nullable : voir TauxTVA
    numero_compte  = Column(String, nullable=False)
    intitule       = Column(String, nullable=False)
    est_defaut     = Column(Boolean, default=False, nullable=False)   # un seul par société
    date_creation  = Column(DateTime, default=datetime.utcnow)

    societe = relationship("Societe")


class ComptesCollectifs(Base):
    """Comptes collectifs du plan comptable (401 Fournisseurs, 411 Clients, 420 Personnel...), propres
    à chaque société — évolution du 2026-08-21, demande Anis. Simple liste de classification : un tiers
    créé (fournisseur/client/salarié) doit être rattaché à l'un d'eux, sans qu'aucune contrainte de
    préfixe numérique ne soit imposée entre le compte collectif et le numéro du tiers rattaché."""
    __tablename__ = "comptes_collectifs"
    __table_args__ = (
        UniqueConstraint("societe_id", "numero_compte", name="uq_compte_collectif_societe_numero"),
    )

    id             = Column(Integer, primary_key=True, index=True)
    societe_id     = Column(Integer, ForeignKey("societes.id"), nullable=False)
    numero_compte  = Column(String, nullable=False)
    intitule       = Column(String, nullable=False)
    date_creation  = Column(DateTime, default=datetime.utcnow)

    societe = relationship("Societe")


# ── Module B : factures achats/ventes (section 5, développé le 2026-08-08) ─────

class Facture(Base):
    __tablename__ = "factures"

    id                  = Column(Integer, primary_key=True, index=True)
    societe_id          = Column(Integer, ForeignKey("societes.id"), nullable=False)
    type                = Column(String, nullable=False)   # achat | vente

    # Tiers rattaché : émetteur pour un achat (fournisseur), destinataire pour une vente (client).
    # Pointe vers la même table tiers que le module relevés (menu déroulant "code + intitulé").
    tiers_id            = Column(Integer, ForeignKey("tiers.id"), nullable=True)

    # Repli quand le vrai fournisseur/client n'est pas encore un tiers connu : un compte récurrent
    # (compte d'attente...) choisi à la place, en alternative à tiers_id — évolution du 2026-08-16,
    # demande Anis. Mutuellement exclusif avec tiers_id côté UI (choisir l'un efface l'autre) ;
    # côté FEC, tiers_id est prioritaire si les deux sont renseignés.
    compte_recurrent_id = Column(Integer, ForeignKey("comptes_recurrents.id"), nullable=True)

    numero              = Column(String, nullable=True)
    date                = Column(Date, nullable=True)
    montant_ht          = Column(Float, nullable=True)
    montant_tva         = Column(Float, nullable=True)
    montant_ttc         = Column(Float, nullable=True)
    nature_prestation   = Column(String, nullable=True)

    # Compte de charge (achat) ou de produit (vente) — un seul champ générique, même mécanisme
    # que la case "Compte comptable" des mouvements (pointe vers tiers, type "compte") — évolution
    # du 2026-08-16.
    compte_id = Column(Integer, ForeignKey("tiers.id"), nullable=True)

    # Taux de TVA choisi pour cette facture, parmi les taux activés pour la société — sert à
    # rattacher le montant_tva déjà connu à son compte comptable de TVA lors de l'export FEC
    # (évolution du 2026-08-16).
    taux_tva_id = Column(Integer, ForeignKey("taux_tva.id"), nullable=True)

    # Date du dernier export FEC incluant cette facture — NULL si jamais exportée (même logique
    # que Mouvement.date_dernier_export) — évolution du 2026-08-16.
    date_dernier_export = Column(DateTime, nullable=True)

    # ok = champs vérifiés/validés par l'utilisateur, a_corriger = juste après extraction IA, pas encore relu
    statut              = Column(String, default="a_corriger")
    fichier_source      = Column(String, nullable=False)   # nom du fichier tel qu'importé (affichage)
    nom_fichier_stocke  = Column(String, nullable=True)    # nom réel sur disque dans factures_files/ (unique)
    date_import         = Column(DateTime, default=datetime.utcnow)

    societe          = relationship("Societe", back_populates="factures")
    tiers            = relationship("Tiers", foreign_keys=[tiers_id])
    compte           = relationship("Tiers", foreign_keys=[compte_id])
    compte_recurrent = relationship("ComptesRecurrents")
    taux_tva         = relationship("TauxTVA")


# ── Journal des anomalies (section 11.3) ─────────────────────────────────────────

class Anomalie(Base):
    __tablename__ = "anomalies"

    id              = Column(Integer, primary_key=True, index=True)
    ref_type        = Column(String, nullable=False)   # releve | facture | general
    ref_id          = Column(Integer, nullable=True)
    description     = Column(String, nullable=False)
    resolue         = Column(Boolean, default=False, nullable=False)
    date_creation   = Column(DateTime, default=datetime.utcnow)
    date_resolution = Column(DateTime, nullable=True)


def init_db():
    Base.metadata.create_all(bind=engine)
    # Migration douce : ajoute les colonnes si une table existe déjà sans elles
    with engine.connect() as conn:
        cols_comptes = [r[1] for r in conn.execute(text("PRAGMA table_info(comptes_bancaires)")).fetchall()]
        if cols_comptes and "mois_debut_exercice" not in cols_comptes:
            conn.execute(text("ALTER TABLE comptes_bancaires ADD COLUMN mois_debut_exercice INTEGER DEFAULT 1"))
            conn.commit()
        for col in ("numero_compte_bancaire", "numero_compte_comptable", "derniers_chiffres", "journal_comptable"):
            if cols_comptes and col not in cols_comptes:
                conn.execute(text(f"ALTER TABLE comptes_bancaires ADD COLUMN {col} VARCHAR"))
                conn.commit()

        cols_mouvements = [r[1] for r in conn.execute(text("PRAGMA table_info(mouvements)")).fetchall()]
        if cols_mouvements and "tiers_id" not in cols_mouvements:
            conn.execute(text("ALTER TABLE mouvements ADD COLUMN tiers_id INTEGER"))
            conn.commit()
        if cols_mouvements and "taux_tva_id" not in cols_mouvements:
            conn.execute(text("ALTER TABLE mouvements ADD COLUMN taux_tva_id INTEGER"))
            conn.commit()
        if cols_mouvements and "date_dernier_export" not in cols_mouvements:
            conn.execute(text("ALTER TABLE mouvements ADD COLUMN date_dernier_export DATETIME"))
            conn.commit()

        cols_factures = [r[1] for r in conn.execute(text("PRAGMA table_info(factures)")).fetchall()]
        if cols_factures and "tiers_id" not in cols_factures:
            conn.execute(text("ALTER TABLE factures ADD COLUMN tiers_id INTEGER"))
            conn.commit()
        if cols_factures and "nom_fichier_stocke" not in cols_factures:
            conn.execute(text("ALTER TABLE factures ADD COLUMN nom_fichier_stocke VARCHAR"))
            conn.commit()
        if cols_factures and "compte_id" not in cols_factures:
            conn.execute(text("ALTER TABLE factures ADD COLUMN compte_id INTEGER"))
            conn.commit()
        if cols_factures and "taux_tva_id" not in cols_factures:
            conn.execute(text("ALTER TABLE factures ADD COLUMN taux_tva_id INTEGER"))
            conn.commit()
        if cols_factures and "date_dernier_export" not in cols_factures:
            conn.execute(text("ALTER TABLE factures ADD COLUMN date_dernier_export DATETIME"))
            conn.commit()
        if cols_factures and "compte_recurrent_id" not in cols_factures:
            conn.execute(text("ALTER TABLE factures ADD COLUMN compte_recurrent_id INTEGER"))
            conn.commit()

        cols_societes = [r[1] for r in conn.execute(text("PRAGMA table_info(societes)")).fetchall()]
        if cols_societes and "prochain_numero_ecriture" not in cols_societes:
            conn.execute(text("ALTER TABLE societes ADD COLUMN prochain_numero_ecriture INTEGER DEFAULT 1"))
            conn.commit()
        if cols_societes and "journal_achats" not in cols_societes:
            conn.execute(text("ALTER TABLE societes ADD COLUMN journal_achats VARCHAR DEFAULT 'ACH'"))
            conn.commit()
        if cols_societes and "journal_ventes" not in cols_societes:
            conn.execute(text("ALTER TABLE societes ADD COLUMN journal_ventes VARCHAR DEFAULT 'VTE'"))
            conn.commit()
        if cols_societes and "validee" not in cols_societes:
            conn.execute(text("ALTER TABLE societes ADD COLUMN validee BOOLEAN DEFAULT 0"))
            conn.commit()

        # Migration ponctuelle : reprend l'ancien compte d'attente unique (parametres_globaux)
        # comme premier compte récurrent par défaut, si ce n'est pas déjà fait.
        if conn.execute(text("SELECT COUNT(*) FROM comptes_recurrents")).scalar() == 0:
            ancien = conn.execute(text(
                "SELECT compte_attente_numero, compte_attente_libelle FROM parametres_globaux WHERE id = 1"
            )).fetchone()
            if ancien and ancien[0]:
                conn.execute(text(
                    "INSERT INTO comptes_recurrents (numero_compte, intitule, est_defaut) VALUES (:num, :lib, 1)"
                ), {"num": ancien[0], "lib": ancien[1] or "Compte d'attente"})
                conn.commit()

        # Évolution du 2026-08-16 (demande Anis) : taux de TVA et comptes récurrents deviennent
        # propres à chaque société (avant : catalogues globaux partagés par toutes les sociétés).
        cols_taux_tva = [r[1] for r in conn.execute(text("PRAGMA table_info(taux_tva)")).fetchall()]
        if cols_taux_tva and "societe_id" not in cols_taux_tva:
            conn.execute(text("ALTER TABLE taux_tva ADD COLUMN societe_id INTEGER"))
            conn.commit()
        cols_comptes_recurrents = [r[1] for r in conn.execute(text("PRAGMA table_info(comptes_recurrents)")).fetchall()]
        if cols_comptes_recurrents and "societe_id" not in cols_comptes_recurrents:
            conn.execute(text("ALTER TABLE comptes_recurrents ADD COLUMN societe_id INTEGER"))
            conn.commit()

        # Rattachement automatique des lignes historiques (sans societe_id) à la société existante,
        # seulement s'il n'y en a qu'une seule — au-delà, impossible de deviner laquelle, à faire
        # manuellement.
        nb_societes = conn.execute(text("SELECT COUNT(*) FROM societes")).scalar()
        if nb_societes == 1:
            societe_unique_id = conn.execute(text("SELECT id FROM societes LIMIT 1")).scalar()
            conn.execute(
                text("UPDATE taux_tva SET societe_id = :sid WHERE societe_id IS NULL"),
                {"sid": societe_unique_id},
            )
            conn.execute(
                text("UPDATE comptes_recurrents SET societe_id = :sid WHERE societe_id IS NULL"),
                {"sid": societe_unique_id},
            )
            conn.commit()

        # Évolution du 2026-08-21 (demande Anis) : comptes collectifs (401/411/420...) par société,
        # et rattachement collectif + journal d'appartenance sur chaque tiers (table comptes_collectifs
        # créée automatiquement par Base.metadata.create_all ci-dessus).
        cols_tiers = [r[1] for r in conn.execute(text("PRAGMA table_info(tiers)")).fetchall()]
        if cols_tiers and "compte_collectif_id" not in cols_tiers:
            conn.execute(text("ALTER TABLE tiers ADD COLUMN compte_collectif_id INTEGER"))
            conn.commit()
        if cols_tiers and "journal" not in cols_tiers:
            conn.execute(text("ALTER TABLE tiers ADD COLUMN journal VARCHAR"))
            conn.commit()


@contextmanager
def get_db():
    db: Session = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
