# app.py
# Backend FastAPI — Application de numérisation (Module A : relevés bancaires)
# Lancer avec : python app.py
#
# Reprise du socle OCR (copie, pas modification en place — voir cadrage section 2).
# Étend l'app OCR d'origine avec : sociétés/comptes, contrôle de solde cumulable,
# blocage des doublons, authentification par session, dashboard, journal d'anomalies.

import io
import secrets
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import openpyxl
import uvicorn
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

sys.path.insert(0, str(Path(__file__).parent))

from database import (
    Anomalie, CompteBancaire, ComptesRecurrents, Facture, Mouvement, Releve,
    Societe, TauxTVA, Tiers, User, get_db, init_db,
)
from auth import (
    aucun_utilisateur_existant, hasher_mot_de_passe,
    utilisateur_courant, verifier_mot_de_passe,
)
from services.extractor import extraire_transactions
from services.exporter import exporter_consolide_excel, exporter_csv, exporter_releve_excel
from services.facture_extractor import EXTENSIONS_IMAGE, ExtractionError, extraire_facture
from services.fec import FECGenerationError, exporter_fec_xlsx, generer_lignes_fec, generer_lignes_fec_factures

FACTURES_DIR = Path(__file__).parent / "factures_files"
FACTURES_DIR.mkdir(exist_ok=True)

TOLERANCE_SOLDE = 0.01  # tolérance d'arrondi pour le contrôle solde initial -> solde final

# ── Secret de session (persistant sur disque, généré une seule fois) ───────────

SECRET_PATH = Path(__file__).parent / ".session_secret"
if not SECRET_PATH.exists():
    SECRET_PATH.write_text(secrets.token_hex(32), encoding="utf-8")
SECRET_KEY = SECRET_PATH.read_text(encoding="utf-8").strip()


@asynccontextmanager
async def lifespan(app):
    init_db()
    yield


app = FastAPI(title="Kwika Numérisation", docs_url="/api/docs", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax", https_only=False)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Affiché dans l'en-tête (voir templates/index.html) pour vérifier en un coup d'œil,
# en cas de problème signalé, si l'installation testée est bien à jour — à incrémenter
# à chaque changement notable poussé sur le dépôt (évolution du 2026-09-03).
VERSION_APP = "2026.09.03-1"


# ── Frontend ──────────────────────────────────────────────────────────────────

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"VERSION_APP": VERSION_APP})


# ── Auth (section 7 du cadrage) ────────────────────────────────────────────────

class LoginPayload(BaseModel):
    email: str
    mot_de_passe: str


@app.get("/api/auth/status")
def auth_status(request: Request):
    setup_requis = aucun_utilisateur_existant()
    user_id = request.session.get("user_id")
    user = None
    if user_id and not setup_requis:
        with get_db() as db:
            u = db.query(User).filter(User.id == user_id, User.actif == True).first()
            if u:
                user = {"id": u.id, "email": u.email, "role": u.role}
    return {"setup_requis": setup_requis, "authentifie": user is not None, "user": user}


@app.post("/api/auth/setup")
def auth_setup(payload: LoginPayload, request: Request):
    """Crée le tout premier compte (admin) — uniquement si aucun utilisateur n'existe."""
    if not aucun_utilisateur_existant():
        raise HTTPException(status_code=403, detail="Un compte existe déjà")
    if len(payload.mot_de_passe) < 8:
        raise HTTPException(status_code=400, detail="Mot de passe trop court (8 caractères minimum)")

    with get_db() as db:
        user = User(email=payload.email.strip().lower(), password_hash=hasher_mot_de_passe(payload.mot_de_passe), role="admin")
        db.add(user)
        db.flush()
        request.session["user_id"] = user.id
        return {"ok": True, "user": {"id": user.id, "email": user.email, "role": user.role}}


@app.post("/api/auth/login")
def auth_login(payload: LoginPayload, request: Request):
    with get_db() as db:
        user = db.query(User).filter(User.email == payload.email.strip().lower(), User.actif == True).first()
        if not user or not verifier_mot_de_passe(payload.mot_de_passe, user.password_hash):
            raise HTTPException(status_code=401, detail="Email ou mot de passe incorrect")
        request.session["user_id"] = user.id
        return {"ok": True, "user": {"id": user.id, "email": user.email, "role": user.role}}


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/auth/me")
def auth_me(user: dict = Depends(utilisateur_courant)):
    return user


# ── Sociétés (section 8) ───────────────────────────────────────────────────────

class SocietePayload(BaseModel):
    nom: str


class SocieteJournauxPayload(BaseModel):
    journal_achats: str
    journal_ventes: str


def _societe_to_dict(s: Societe) -> dict:
    return {
        "id": s.id, "nom": s.nom, "nb_comptes": len(s.comptes),
        "journal_achats": s.journal_achats, "journal_ventes": s.journal_ventes,
        "validee": s.validee,
    }


@app.get("/api/societes")
def get_societes(user: dict = Depends(utilisateur_courant)):
    with get_db() as db:
        societes = db.query(Societe).order_by(Societe.nom).all()
        return [_societe_to_dict(s) for s in societes]


@app.post("/api/societes")
def creer_societe(payload: SocietePayload, user: dict = Depends(utilisateur_courant)):
    nom = payload.nom.strip()
    if not nom:
        raise HTTPException(status_code=400, detail="Nom requis")
    with get_db() as db:
        if db.query(Societe).filter(Societe.nom == nom).first():
            raise HTTPException(status_code=409, detail="Cette société existe déjà")
        s = Societe(nom=nom)
        db.add(s)
        db.flush()
        return _societe_to_dict(s)


@app.patch("/api/societes/{societe_id}/journaux")
def modifier_journaux_societe(societe_id: int, payload: SocieteJournauxPayload, user: dict = Depends(utilisateur_courant)):
    """Codes journaux utilisés pour les écritures FEC des factures (achats/ventes) — évolution
    du 2026-08-16. Le module relevés a son propre journal_comptable, par compte bancaire."""
    with get_db() as db:
        s = db.query(Societe).filter(Societe.id == societe_id).first()
        if not s:
            raise HTTPException(status_code=404, detail="Société introuvable")
        s.journal_achats = payload.journal_achats.strip() or "ACH"
        s.journal_ventes = payload.journal_ventes.strip() or "VTE"
        return _societe_to_dict(s)


@app.post("/api/societes/{societe_id}/valider")
def valider_societe(societe_id: int, user: dict = Depends(utilisateur_courant)):
    """Valide la société (évolution du 2026-08-20, demande Anis) : une société ne peut être
    validée que si elle a au moins un compte tiers Fournisseur, un Client et un Personnel
    (numéro + libellé déjà garantis non vides par la création de tiers) — sinon renvoie
    l'élément manquant, à afficher tel quel dans une fenêtre côté frontend."""
    with get_db() as db:
        s = db.query(Societe).filter(Societe.id == societe_id).first()
        if not s:
            raise HTTPException(status_code=404, detail="Société introuvable")
        manquants = []
        if not db.query(Tiers).filter(Tiers.societe_id == societe_id, Tiers.type == "fournisseur").first():
            manquants.append("un compte Fournisseur")
        if not db.query(Tiers).filter(Tiers.societe_id == societe_id, Tiers.type == "client").first():
            manquants.append("un compte Client")
        if not db.query(Tiers).filter(Tiers.societe_id == societe_id, Tiers.type == "salarie").first():
            manquants.append("un compte Personnel")
        if manquants:
            raise HTTPException(
                status_code=400,
                detail="Impossible de valider la société — il manque au moins " + ", ".join(manquants) + ".",
            )
        s.validee = True
        return _societe_to_dict(s)


@app.delete("/api/societes/{societe_id}")
def supprimer_societe(societe_id: int, user: dict = Depends(utilisateur_courant)):
    with get_db() as db:
        s = db.query(Societe).filter(Societe.id == societe_id).first()
        if not s:
            raise HTTPException(status_code=404, detail="Société introuvable")
        # Tiers n'est pas rattaché à Societe par une relation ORM en cascade — suppression explicite
        # pour éviter de laisser des comptes tiers orphelins (societe_id pointant sur rien).
        db.query(Tiers).filter(Tiers.societe_id == societe_id).delete()
        db.delete(s)
    return {"ok": True}


# ── Comptes bancaires (section 8) ──────────────────────────────────────────────

class ComptePayload(BaseModel):
    societe_id: int
    banque: str
    libelle: str
    devise: str = "EUR"
    mois_debut_exercice: int = 1
    numero_compte_bancaire: Optional[str] = None
    numero_compte_comptable: Optional[str] = None
    derniers_chiffres: Optional[str] = None
    journal_comptable: Optional[str] = None


def _compte_to_dict(c: CompteBancaire) -> dict:
    return {
        "id": c.id, "societe_id": c.societe_id, "banque": c.banque,
        "libelle": c.libelle, "devise": c.devise,
        "mois_debut_exercice": c.mois_debut_exercice,
        "numero_compte_bancaire": c.numero_compte_bancaire,
        "numero_compte_comptable": c.numero_compte_comptable,
        "derniers_chiffres": c.derniers_chiffres,
        "journal_comptable": c.journal_comptable,
        "nb_releves": len(c.releves),
    }


@app.get("/api/comptes")
def get_comptes(societe_id: Optional[int] = Query(None), user: dict = Depends(utilisateur_courant)):
    with get_db() as db:
        q = db.query(CompteBancaire)
        if societe_id:
            q = q.filter(CompteBancaire.societe_id == societe_id)
        comptes = q.order_by(CompteBancaire.libelle).all()
        return [_compte_to_dict(c) for c in comptes]


@app.post("/api/comptes")
def creer_compte(payload: ComptePayload, user: dict = Depends(utilisateur_courant)):
    if not (1 <= payload.mois_debut_exercice <= 12):
        raise HTTPException(status_code=400, detail="Mois de début d'exercice invalide")
    if payload.derniers_chiffres and len(payload.derniers_chiffres.strip()) != 4:
        raise HTTPException(status_code=400, detail="Les 4 derniers chiffres doivent contenir exactement 4 caractères")
    with get_db() as db:
        if not db.query(Societe).filter(Societe.id == payload.societe_id).first():
            raise HTTPException(status_code=404, detail="Société introuvable")
        c = CompteBancaire(
            societe_id=payload.societe_id, banque=payload.banque.strip(),
            libelle=payload.libelle.strip(), devise=payload.devise,
            mois_debut_exercice=payload.mois_debut_exercice,
            numero_compte_bancaire=(payload.numero_compte_bancaire or "").strip() or None,
            numero_compte_comptable=(payload.numero_compte_comptable or "").strip() or None,
            derniers_chiffres=(payload.derniers_chiffres or "").strip() or None,
            journal_comptable=(payload.journal_comptable or "").strip() or None,
        )
        db.add(c)
        db.flush()
        return {"id": c.id}


@app.delete("/api/comptes/{compte_id}")
def supprimer_compte(compte_id: int, user: dict = Depends(utilisateur_courant)):
    with get_db() as db:
        c = db.query(CompteBancaire).filter(CompteBancaire.id == compte_id).first()
        if not c:
            raise HTTPException(status_code=404, detail="Compte introuvable")
        db.delete(c)
    return {"ok": True}


# ── Comptes récurrents : compte d'attente + comptes fixes (frais bancaires, ────
# prélèvements...), propres à chaque société (évolution du 2026-08-16, demande Anis — ─────
# remplace la liste globale partagée : tout ce qui concerne une société est géré depuis ──
# l'onglet Sociétés & comptes) ──────────────────────────────────────────────────

class CompteRecurrentPayload(BaseModel):
    societe_id: int
    numero_compte: str
    intitule: str
    est_defaut: bool = False


def _compte_recurrent_to_dict(c: ComptesRecurrents) -> dict:
    return {
        "id": c.id, "societe_id": c.societe_id,
        "numero_compte": c.numero_compte, "intitule": c.intitule, "est_defaut": c.est_defaut,
    }


@app.get("/api/comptes-recurrents")
def get_comptes_recurrents(societe_id: int = Query(...), user: dict = Depends(utilisateur_courant)):
    with get_db() as db:
        comptes = (
            db.query(ComptesRecurrents)
            .filter(ComptesRecurrents.societe_id == societe_id)
            .order_by(ComptesRecurrents.intitule)
            .all()
        )
        return [_compte_recurrent_to_dict(c) for c in comptes]


@app.post("/api/comptes-recurrents")
def creer_compte_recurrent(payload: CompteRecurrentPayload, user: dict = Depends(utilisateur_courant)):
    numero = payload.numero_compte.strip()
    intitule = payload.intitule.strip()
    if not numero or not intitule:
        raise HTTPException(status_code=400, detail="Numéro de compte et intitulé requis")
    with get_db() as db:
        if not db.query(Societe).filter(Societe.id == payload.societe_id).first():
            raise HTTPException(status_code=404, detail="Société introuvable")
        if db.query(ComptesRecurrents).filter(
            ComptesRecurrents.societe_id == payload.societe_id, ComptesRecurrents.numero_compte == numero,
        ).first():
            raise HTTPException(status_code=409, detail="Ce numéro de compte existe déjà pour cette société")
        if payload.est_defaut:
            db.query(ComptesRecurrents).filter(ComptesRecurrents.societe_id == payload.societe_id).update(
                {ComptesRecurrents.est_defaut: False}
            )
        c = ComptesRecurrents(
            societe_id=payload.societe_id, numero_compte=numero, intitule=intitule, est_defaut=payload.est_defaut,
        )
        db.add(c)
        db.flush()
        return _compte_recurrent_to_dict(c)


@app.patch("/api/comptes-recurrents/{compte_id}/defaut")
def definir_compte_recurrent_defaut(compte_id: int, user: dict = Depends(utilisateur_courant)):
    with get_db() as db:
        c = db.query(ComptesRecurrents).filter(ComptesRecurrents.id == compte_id).first()
        if not c:
            raise HTTPException(status_code=404, detail="Compte introuvable")
        db.query(ComptesRecurrents).filter(ComptesRecurrents.societe_id == c.societe_id).update(
            {ComptesRecurrents.est_defaut: False}
        )
        c.est_defaut = True
    return {"ok": True}


@app.delete("/api/comptes-recurrents/{compte_id}")
def supprimer_compte_recurrent(compte_id: int, user: dict = Depends(utilisateur_courant)):
    with get_db() as db:
        c = db.query(ComptesRecurrents).filter(ComptesRecurrents.id == compte_id).first()
        if not c:
            raise HTTPException(status_code=404, detail="Compte introuvable")
        if c.est_defaut:
            raise HTTPException(
                status_code=400,
                detail="Ce compte est le repli par défaut — définissez un autre compte par défaut avant de le supprimer.",
            )
        db.delete(c)
    return {"ok": True}


# ── Taux de TVA et comptes associés, propres à chaque société (évolution du 2026-08-16, ──
# demande Anis — remplace le catalogue global partagé + activation par société) ──────────

class TauxTVAPayload(BaseModel):
    societe_id: int
    taux: float
    numero_compte_tva: str
    intitule: str


def _taux_tva_to_dict(t: TauxTVA) -> dict:
    return {
        "id": t.id, "societe_id": t.societe_id,
        "taux": t.taux, "numero_compte_tva": t.numero_compte_tva, "intitule": t.intitule,
    }


@app.get("/api/taux-tva")
def get_taux_tva(societe_id: int = Query(...), user: dict = Depends(utilisateur_courant)):
    with get_db() as db:
        taux = (
            db.query(TauxTVA)
            .filter(TauxTVA.societe_id == societe_id)
            .order_by(TauxTVA.taux.desc())
            .all()
        )
        return [_taux_tva_to_dict(t) for t in taux]


@app.post("/api/taux-tva")
def creer_taux_tva(payload: TauxTVAPayload, user: dict = Depends(utilisateur_courant)):
    with get_db() as db:
        if not db.query(Societe).filter(Societe.id == payload.societe_id).first():
            raise HTTPException(status_code=404, detail="Société introuvable")
        if db.query(TauxTVA).filter(TauxTVA.societe_id == payload.societe_id, TauxTVA.taux == payload.taux).first():
            raise HTTPException(status_code=409, detail="Ce taux de TVA existe déjà pour cette société")
        t = TauxTVA(
            societe_id=payload.societe_id, taux=payload.taux,
            numero_compte_tva=payload.numero_compte_tva.strip(), intitule=payload.intitule.strip(),
        )
        db.add(t)
        db.flush()
        return _taux_tva_to_dict(t)


class TauxTVAModificationPayload(BaseModel):
    taux: float
    numero_compte_tva: str
    intitule: str


@app.patch("/api/taux-tva/{taux_id}")
def modifier_taux_tva(taux_id: int, payload: TauxTVAModificationPayload, user: dict = Depends(utilisateur_courant)):
    with get_db() as db:
        t = db.query(TauxTVA).filter(TauxTVA.id == taux_id).first()
        if not t:
            raise HTTPException(status_code=404, detail="Taux de TVA introuvable")
        t.taux = payload.taux
        t.numero_compte_tva = payload.numero_compte_tva.strip()
        t.intitule = payload.intitule.strip()
        return _taux_tva_to_dict(t)


@app.delete("/api/taux-tva/{taux_id}")
def supprimer_taux_tva(taux_id: int, user: dict = Depends(utilisateur_courant)):
    with get_db() as db:
        t = db.query(TauxTVA).filter(TauxTVA.id == taux_id).first()
        if not t:
            raise HTTPException(status_code=404, detail="Taux de TVA introuvable")
        db.query(Mouvement).filter(Mouvement.taux_tva_id == taux_id).update({Mouvement.taux_tva_id: None})
        db.query(Facture).filter(Facture.taux_tva_id == taux_id).update({Facture.taux_tva_id: None})
        db.delete(t)
    return {"ok": True}


# ── Comptes tiers (clients/fournisseurs) — liste par société (évolution du 2026-08-03) ──

@app.get("/api/tiers")
def get_tiers(
    societe_id: int = Query(...),
    type: Optional[str] = Query(None, description="client | fournisseur"),
    q: Optional[str] = Query(None, description="recherche par intitulé ou numéro (3-4 lettres min.)"),
    user: dict = Depends(utilisateur_courant),
):
    with get_db() as db:
        req = db.query(Tiers).filter(Tiers.societe_id == societe_id)
        if type:
            req = req.filter(Tiers.type == type)
        if q:
            motif = f"%{q.strip()}%"
            req = req.filter((Tiers.intitule.ilike(motif)) | (Tiers.numero_compte.ilike(motif)))
        tiers = req.order_by(Tiers.intitule).all()
        return [
            {"id": t.id, "numero_compte": t.numero_compte, "intitule": t.intitule, "type": t.type}
            for t in tiers
        ]


class TiersPayload(BaseModel):
    societe_id: int
    numero_compte: str
    intitule: str
    type: str  # client | fournisseur | salarie | compte


@app.post("/api/tiers")
def creer_tiers(payload: TiersPayload, user: dict = Depends(utilisateur_courant)):
    """Création unitaire d'un compte tiers (évolution du 2026-08-16, demande Anis) — jusqu'ici
    seul l'import Excel en masse existait ; utile pour créer à la volée un fournisseur/client
    absent de la liste, directement depuis le formulaire facture."""
    if payload.type not in ("client", "fournisseur", "salarie", "compte"):
        raise HTTPException(status_code=400, detail="Type invalide")
    numero = payload.numero_compte.strip()
    intitule = payload.intitule.strip()
    if not numero or not intitule:
        raise HTTPException(status_code=400, detail="Numéro et intitulé requis")
    if any(c.isspace() for c in numero):
        raise HTTPException(status_code=400, detail="Le numéro de compte tiers ne doit contenir aucun espace")
    with get_db() as db:
        if not db.query(Societe).filter(Societe.id == payload.societe_id).first():
            raise HTTPException(status_code=404, detail="Société introuvable")
        if db.query(Tiers).filter(Tiers.societe_id == payload.societe_id, Tiers.numero_compte == numero).first():
            raise HTTPException(status_code=409, detail="Un compte avec ce numéro existe déjà pour cette société")
        t = Tiers(societe_id=payload.societe_id, numero_compte=numero, intitule=intitule, type=payload.type)
        db.add(t)
        db.flush()
        return {"id": t.id, "numero_compte": t.numero_compte, "intitule": t.intitule, "type": t.type}


@app.post("/api/tiers/import")
async def importer_tiers(
    societe_id: int = Form(...),
    file: UploadFile = File(...),
    user: dict = Depends(utilisateur_courant),
):
    if not file.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="La liste de tiers doit être un fichier Excel")

    contenu = await file.read()
    try:
        wb = openpyxl.load_workbook(io.BytesIO(contenu), read_only=True, data_only=True)
        ws = wb.active
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Fichier Excel illisible : {e}")

    with get_db() as db:
        if not db.query(Societe).filter(Societe.id == societe_id).first():
            raise HTTPException(status_code=404, detail="Société introuvable")

        # Dédoublonnage en mémoire d'abord (le fichier réel peut contenir des numéros répétés) —
        # en cas de doublon dans le fichier, la dernière occurrence l'emporte.
        lignes: dict[str, tuple[str, str]] = {}
        ignores = 0
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or row[0] is None:
                continue
            numero = str(row[0]).strip()
            intitule = str(row[1]).strip() if len(row) > 1 and row[1] is not None else ""
            type_brut = str(row[2]).strip().lower() if len(row) > 2 and row[2] is not None else ""
            if type_brut.startswith("client"):
                type_tiers = "client"
            elif type_brut.startswith("fournisseur"):
                type_tiers = "fournisseur"
            elif type_brut.startswith("salari"):
                type_tiers = "salarie"
            elif type_brut.startswith(("compte", "charge", "produit")):
                type_tiers = "compte"
            else:
                ignores += 1
                continue
            if not numero or not intitule:
                ignores += 1
                continue
            lignes[numero] = (intitule, type_tiers)

        existants = {
            t.numero_compte: t
            for t in db.query(Tiers).filter(Tiers.societe_id == societe_id).all()
        }
        crees, maj = 0, 0
        for numero, (intitule, type_tiers) in lignes.items():
            existant = existants.get(numero)
            if existant:
                existant.intitule = intitule
                existant.type = type_tiers
                maj += 1
            else:
                db.add(Tiers(societe_id=societe_id, numero_compte=numero, intitule=intitule, type=type_tiers))
                crees += 1

        return {"crees": crees, "mis_a_jour": maj, "ignores": ignores}


# ── Rattachement de la contrepartie d'une ligne de relevé (évolution du 2026-08-03) ──
# Une seule case côté relevé (tiers_id), qui pointe vers une entrée de la table tiers —
# qu'elle soit un tiers (client/fournisseur/salarié) ou un compte comptable ordinaire (type "compte").
# Les deux cases visibles côté frontend (Tiers / Compte comptable) écrivent donc le même champ.
# Aucun rattachement -> repli automatique sur le compte récurrent par défaut à l'export FEC.
# taux_tva_id : choix manuel et optionnel, uniquement valable quand tiers_id pointe vers un compte
# de type "compte" — scinde alors le montant du mouvement en HT + TVA à l'export FEC.

class RattachementContrepartiePayload(BaseModel):
    tiers_id: Optional[int] = None
    taux_tva_id: Optional[int] = None


@app.patch("/api/mouvements/{mouvement_id}/contrepartie")
def rattacher_contrepartie(mouvement_id: int, payload: RattachementContrepartiePayload, user: dict = Depends(utilisateur_courant)):
    with get_db() as db:
        m = db.query(Mouvement).filter(Mouvement.id == mouvement_id).first()
        if not m:
            raise HTTPException(status_code=404, detail="Mouvement introuvable")

        tiers = None
        if payload.tiers_id is not None:
            tiers = db.query(Tiers).filter(Tiers.id == payload.tiers_id).first()
            if not tiers:
                raise HTTPException(status_code=404, detail="Tiers introuvable")

        if payload.taux_tva_id is not None:
            if not tiers or tiers.type != "compte":
                raise HTTPException(status_code=400, detail="Un compte TVA ne peut être choisi que pour un compte comptable ordinaire")
            societe_id = m.releve.compte.societe_id
            if not db.query(TauxTVA).filter(TauxTVA.id == payload.taux_tva_id, TauxTVA.societe_id == societe_id).first():
                raise HTTPException(status_code=404, detail="Taux de TVA introuvable pour cette société")

        m.tiers_id = payload.tiers_id
        m.taux_tva_id = payload.taux_tva_id
    return {"ok": True}


# ── Relevés bancaires : upload + contrôle solde + doublons (section 4) ─────────

def _suggestion_solde_initial(db, compte_id: int, mois: int, annee: int) -> Optional[float]:
    """
    Reprend le solde final calculé du dernier relevé validé (statut ok) strictement
    antérieur à la période demandée — même s'il y a des mois manquants entre les deux
    (ex. juillet 2025 -> février 2026 si aucun mois entre les deux n'a été déposé).
    """
    releve_prec = (
        db.query(Releve)
        .filter(
            Releve.compte_id == compte_id,
            Releve.statut == "ok",
            (Releve.annee < annee) | ((Releve.annee == annee) & (Releve.mois < mois)),
        )
        .order_by(Releve.annee.desc(), Releve.mois.desc())
        .first()
    )
    return releve_prec.solde_final_calcule if releve_prec else None


@app.get("/api/comptes/{compte_id}/suggestion-solde-initial")
def suggestion_solde_initial(compte_id: int, mois: int = Query(...), annee: int = Query(...), user: dict = Depends(utilisateur_courant)):
    with get_db() as db:
        return {"solde_initial_suggere": _suggestion_solde_initial(db, compte_id, mois, annee)}


@app.post("/api/releves/upload")
async def upload_releve(
    compte_id: int = Form(...),
    mois: int = Form(...),
    annee: int = Form(...),
    solde_final_attendu: float = Form(...),
    solde_initial: Optional[float] = Form(None),
    force: bool = Form(False),
    file: UploadFile = File(...),
    user: dict = Depends(utilisateur_courant),
):
    if not (1 <= mois <= 12):
        raise HTTPException(status_code=400, detail="Mois invalide")
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Le relevé doit être un PDF")

    with get_db() as db:
        compte = db.query(CompteBancaire).filter(CompteBancaire.id == compte_id).first()
        if not compte:
            raise HTTPException(status_code=404, detail="Compte introuvable")
        compte_libelle = compte.libelle

        existant = (
            db.query(Releve)
            .filter(Releve.compte_id == compte_id, Releve.mois == mois, Releve.annee == annee)
            .first()
        )
        if existant and not force:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Un relevé pour {mois:02d}/{annee} existe déjà pour ce compte "
                    f"(statut: {existant.statut}). Confirmez pour écraser et remplacer."
                ),
            )

        solde_depart = solde_initial
        if solde_depart is None:
            solde_depart = _suggestion_solde_initial(db, compte_id, mois, annee)
        if solde_depart is None:
            raise HTTPException(
                status_code=400,
                detail="Solde initial requis (aucun relevé validé du mois précédent à reprendre).",
            )

    # Sauvegarde temporaire sur disque (pdfplumber a besoin d'un chemin)
    contenu = await file.read()
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(contenu)
        chemin_tmp = tmp.name

    try:
        transactions, banque_detectee = extraire_transactions(chemin_tmp)
    except Exception as e:
        Path(chemin_tmp).unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=f"Échec de l'extraction : {e}")
    finally:
        Path(chemin_tmp).unlink(missing_ok=True)

    if not transactions:
        raise HTTPException(status_code=422, detail="Aucun mouvement extrait — format non reconnu")

    total_debit = sum(t.debit or 0.0 for t in transactions)
    total_credit = sum(t.credit or 0.0 for t in transactions)
    solde_calcule = round(solde_depart + total_credit - total_debit, 2)
    ecart = round(solde_calcule - solde_final_attendu, 2)
    statut = "ok" if abs(ecart) <= TOLERANCE_SOLDE else "ecart"

    with get_db() as db:
        existant = (
            db.query(Releve)
            .filter(Releve.compte_id == compte_id, Releve.mois == mois, Releve.annee == annee)
            .first()
        )
        if existant:
            db.delete(existant)
            db.flush()

        releve = Releve(
            compte_id=compte_id, mois=mois, annee=annee,
            nom_fichier=file.filename, banque_detectee=banque_detectee,
            solde_initial=solde_depart, solde_final_attendu=solde_final_attendu,
            solde_final_calcule=solde_calcule, statut=statut,
            nb_mouvements=len(transactions),
        )
        db.add(releve)
        db.flush()

        for t in transactions:
            db.add(Mouvement(
                releve_id=releve.id, date=t.date, libelle=t.libelle,
                debit=t.debit, credit=t.credit, categorie=t.categorie,
            ))

        if statut == "ecart":
            db.add(Anomalie(
                ref_type="releve", ref_id=releve.id,
                description=(
                    f"Écart de solde sur le relevé {mois:02d}/{annee} ({compte_libelle}) : "
                    f"calculé {solde_calcule:.2f} vs attendu {solde_final_attendu:.2f} "
                    f"(écart {ecart:+.2f})."
                ),
            ))

        return {
            "releve_id": releve.id,
            "statut": statut,
            "banque_detectee": banque_detectee,
            "nb_mouvements": len(transactions),
            "solde_initial": solde_depart,
            "solde_final_attendu": solde_final_attendu,
            "solde_final_calcule": solde_calcule,
            "ecart": ecart,
            "alerte": statut == "ecart",
        }


@app.get("/api/releves")
def get_releves(
    societe_id: Optional[int] = Query(None),
    compte_id: Optional[int] = Query(None),
    statut: Optional[str] = Query(None),
    user: dict = Depends(utilisateur_courant),
):
    with get_db() as db:
        q = db.query(Releve).join(CompteBancaire)
        if compte_id:
            q = q.filter(Releve.compte_id == compte_id)
        if societe_id:
            q = q.filter(CompteBancaire.societe_id == societe_id)
        if statut:
            q = q.filter(Releve.statut == statut)
        releves = q.order_by(Releve.annee.desc(), Releve.mois.desc()).all()
        return [
            {
                "id": r.id, "compte_id": r.compte_id, "mois": r.mois, "annee": r.annee,
                "nom_fichier": r.nom_fichier, "banque_detectee": r.banque_detectee,
                "date_import": r.date_import.isoformat() if r.date_import else None,
                "solde_initial": r.solde_initial, "solde_final_attendu": r.solde_final_attendu,
                "solde_final_calcule": r.solde_final_calcule, "statut": r.statut,
                "nb_mouvements": r.nb_mouvements,
            }
            for r in releves
        ]


@app.get("/api/releves/{releve_id}/mouvements")
def get_mouvements(releve_id: int, user: dict = Depends(utilisateur_courant)):
    with get_db() as db:
        releve = db.query(Releve).filter(Releve.id == releve_id).first()
        if not releve:
            raise HTTPException(status_code=404, detail="Relevé introuvable")
        mouvements = db.query(Mouvement).filter(Mouvement.releve_id == releve_id).order_by(Mouvement.date).all()
        return [
            {
                "id": m.id, "date": m.date.isoformat() if m.date else None,
                "libelle": m.libelle, "debit": m.debit, "credit": m.credit,
                "categorie": m.categorie, "tiers_id": m.tiers_id, "taux_tva_id": m.taux_tva_id,
                "tiers": {"id": m.tiers.id, "numero_compte": m.tiers.numero_compte, "intitule": m.tiers.intitule, "type": m.tiers.type} if m.tiers else None,
                "taux_tva": _taux_tva_to_dict(m.taux_tva) if m.taux_tva else None,
                "date_dernier_export": m.date_dernier_export.isoformat() if m.date_dernier_export else None,
            }
            for m in mouvements
        ]


# ── Sélection de mouvements pour l'export + marquage "déjà exporté" (évolution du 2026-08-04) ──

def _filtrer_mouvements_selection(mouvements: list[Mouvement], mouvement_ids: Optional[str]) -> list[Mouvement]:
    """Filtre sur une liste d'IDs séparés par des virgules si fournie, sinon retourne tout."""
    if not mouvement_ids:
        return mouvements
    try:
        ids = {int(x) for x in mouvement_ids.split(",") if x.strip()}
    except ValueError:
        raise HTTPException(status_code=400, detail="Liste de mouvements invalide")
    return [m for m in mouvements if m.id in ids]


def _marquer_exportes(db, mouvements: list[Mouvement]):
    maintenant = datetime.utcnow()
    for m in mouvements:
        m.date_dernier_export = maintenant


@app.delete("/api/releves/{releve_id}")
def supprimer_releve(releve_id: int, user: dict = Depends(utilisateur_courant)):
    with get_db() as db:
        releve = db.query(Releve).filter(Releve.id == releve_id).first()
        if not releve:
            raise HTTPException(status_code=404, detail="Relevé introuvable")
        db.delete(releve)
    return {"ok": True}


# ── Export Excel (extraction à la demande depuis la base — section 2) ─────────

@app.get("/api/releves/{releve_id}/export")
def export_releve(releve_id: int, mouvement_ids: Optional[str] = Query(None), user: dict = Depends(utilisateur_courant)):
    with get_db() as db:
        releve = db.query(Releve).filter(Releve.id == releve_id).first()
        if not releve:
            raise HTTPException(status_code=404, detail="Relevé introuvable")
        compte = db.query(CompteBancaire).filter(CompteBancaire.id == releve.compte_id).first()
        societe = db.query(Societe).filter(Societe.id == compte.societe_id).first()
        mouvements = db.query(Mouvement).filter(Mouvement.releve_id == releve_id).order_by(Mouvement.date).all()
        mouvements = _filtrer_mouvements_selection(mouvements, mouvement_ids)
        if not mouvements:
            raise HTTPException(status_code=400, detail="Aucun mouvement sélectionné")
        buf = exporter_releve_excel(societe.nom, compte.libelle, releve, mouvements)
        nom = f"releve_{compte.libelle}_{releve.mois:02d}-{releve.annee}.xlsx".replace(" ", "-")
        _marquer_exportes(db, mouvements)

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nom}"'},
    )


@app.get("/api/comptes/{compte_id}/export-consolide")
def export_consolide(compte_id: int, annee_exercice: int = Query(...), user: dict = Depends(utilisateur_courant)):
    """Extraction consolidée des 12 mois de l'exercice démarrant en annee_exercice (section 4.4)."""
    with get_db() as db:
        compte = db.query(CompteBancaire).filter(CompteBancaire.id == compte_id).first()
        if not compte:
            raise HTTPException(status_code=404, detail="Compte introuvable")
        societe = db.query(Societe).filter(Societe.id == compte.societe_id).first()

        periodes = []
        mois, annee = compte.mois_debut_exercice, annee_exercice
        for _ in range(12):
            periodes.append((mois, annee))
            mois, annee = (1, annee + 1) if mois == 12 else (mois + 1, annee)

        releves_mouvements = []
        for m, a in periodes:
            releve = db.query(Releve).filter(Releve.compte_id == compte_id, Releve.mois == m, Releve.annee == a).first()
            if releve:
                mouvements = db.query(Mouvement).filter(Mouvement.releve_id == releve.id).order_by(Mouvement.date).all()
                releves_mouvements.append((releve, mouvements))

        if not releves_mouvements:
            raise HTTPException(status_code=404, detail="Aucun relevé pour cet exercice")

        buf = exporter_consolide_excel(societe.nom, compte.libelle, releves_mouvements)
        nom = f"exercice_{compte.libelle}_{annee_exercice}.xlsx".replace(" ", "-")
        for _, mouvements in releves_mouvements:
            _marquer_exportes(db, mouvements)

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nom}"'},
    )


@app.get("/api/releves/{releve_id}/export-csv")
def export_releve_csv(releve_id: int, mouvement_ids: Optional[str] = Query(None), user: dict = Depends(utilisateur_courant)):
    with get_db() as db:
        releve = db.query(Releve).filter(Releve.id == releve_id).first()
        if not releve:
            raise HTTPException(status_code=404, detail="Relevé introuvable")
        mouvements = db.query(Mouvement).filter(Mouvement.releve_id == releve_id).order_by(Mouvement.date).all()
        mouvements = _filtrer_mouvements_selection(mouvements, mouvement_ids)
        if not mouvements:
            raise HTTPException(status_code=400, detail="Aucun mouvement sélectionné")
        buf = exporter_csv(mouvements)
        nom = f"releve_{releve.mois:02d}-{releve.annee}.csv"
        _marquer_exportes(db, mouvements)

    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f'attachment; filename="{nom}"'},
    )


# ── Export FEC (écriture à double entrée, évolution du 2026-08-03) ─────────────
# Numérotation d'écriture (évolution du 2026-08-16) : un compteur par société
# (Societe.prochain_numero_ecriture), partagé entre le FEC des relevés et celui des factures.

def _charger_compte_recurrent_defaut(db, societe_id: int) -> tuple[str, str]:
    c = db.query(ComptesRecurrents).filter(
        ComptesRecurrents.societe_id == societe_id, ComptesRecurrents.est_defaut == True,
    ).first()
    if not c:
        raise HTTPException(
            status_code=400,
            detail="Aucun compte récurrent par défaut configuré pour cette société — définissez-en un dans "
            "Sociétés & comptes avant d'exporter.",
        )
    return c.numero_compte, c.intitule


def _numero_ecriture_depart(db, societe_id: int) -> int:
    s = db.query(Societe).filter(Societe.id == societe_id).first()
    return s.prochain_numero_ecriture if s else 1


def _persister_numero_ecriture(db, societe_id: int, prochain: int):
    db.query(Societe).filter(Societe.id == societe_id).update({Societe.prochain_numero_ecriture: prochain})


@app.get("/api/releves/{releve_id}/export-fec")
def export_fec_releve(releve_id: int, mouvement_ids: Optional[str] = Query(None), user: dict = Depends(utilisateur_courant)):
    with get_db() as db:
        releve = db.query(Releve).filter(Releve.id == releve_id).first()
        if not releve:
            raise HTTPException(status_code=404, detail="Relevé introuvable")
        compte = db.query(CompteBancaire).filter(CompteBancaire.id == releve.compte_id).first()
        if not compte.numero_compte_comptable or not compte.journal_comptable:
            raise HTTPException(
                status_code=400,
                detail="Ce compte bancaire n'a pas de compte comptable / journal renseigné — complétez sa fiche avant d'exporter.",
            )
        compte_defaut_numero, compte_defaut_libelle = _charger_compte_recurrent_defaut(db, compte.societe_id)
        mouvements = db.query(Mouvement).filter(Mouvement.releve_id == releve_id).order_by(Mouvement.date).all()
        mouvements = _filtrer_mouvements_selection(mouvements, mouvement_ids)
        if not mouvements:
            raise HTTPException(status_code=400, detail="Aucun mouvement sélectionné")

        numero_depart = _numero_ecriture_depart(db, compte.societe_id)
        try:
            lignes, prochain = generer_lignes_fec(
                compte, [(m, releve) for m in mouvements], compte_defaut_numero, compte_defaut_libelle, numero_depart,
            )
        except FECGenerationError as e:
            raise HTTPException(status_code=400, detail=str(e))
        buf = exporter_fec_xlsx(lignes)
        nom = f"Export_{compte.libelle}_{releve.mois:02d}-{releve.annee}.xlsx".replace(" ", "-")
        _marquer_exportes(db, mouvements)
        _persister_numero_ecriture(db, compte.societe_id, prochain)

    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nom}"'},
    )


@app.get("/api/comptes/{compte_id}/export-fec-exercice")
def export_fec_exercice(compte_id: int, annee_exercice: int = Query(...), user: dict = Depends(utilisateur_courant)):
    with get_db() as db:
        compte = db.query(CompteBancaire).filter(CompteBancaire.id == compte_id).first()
        if not compte:
            raise HTTPException(status_code=404, detail="Compte introuvable")
        if not compte.numero_compte_comptable or not compte.journal_comptable:
            raise HTTPException(
                status_code=400,
                detail="Ce compte bancaire n'a pas de compte comptable / journal renseigné — complétez sa fiche avant d'exporter.",
            )
        compte_defaut_numero, compte_defaut_libelle = _charger_compte_recurrent_defaut(db, compte.societe_id)

        periodes = []
        mois, annee = compte.mois_debut_exercice, annee_exercice
        for _ in range(12):
            periodes.append((mois, annee))
            mois, annee = (1, annee + 1) if mois == 12 else (mois + 1, annee)

        mouvements_avec_releve = []
        for m, a in periodes:
            releve = db.query(Releve).filter(Releve.compte_id == compte_id, Releve.mois == m, Releve.annee == a).first()
            if releve:
                mouvements = db.query(Mouvement).filter(Mouvement.releve_id == releve.id).order_by(Mouvement.date).all()
                mouvements_avec_releve.extend((mv, releve) for mv in mouvements)

        if not mouvements_avec_releve:
            raise HTTPException(status_code=404, detail="Aucun relevé pour cet exercice")

        numero_depart = _numero_ecriture_depart(db, compte.societe_id)
        try:
            lignes, prochain = generer_lignes_fec(
                compte, mouvements_avec_releve, compte_defaut_numero, compte_defaut_libelle, numero_depart,
            )
        except FECGenerationError as e:
            raise HTTPException(status_code=400, detail=str(e))
        buf = exporter_fec_xlsx(lignes)
        nom = f"Export_{compte.libelle}_exercice-{annee_exercice}.xlsx".replace(" ", "-")
        _marquer_exportes(db, [mv for mv, _ in mouvements_avec_releve])
        _persister_numero_ecriture(db, compte.societe_id, prochain)

    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nom}"'},
    )


# ── Dashboard (section 10) ──────────────────────────────────────────────────────

@app.get("/api/dashboard")
def get_dashboard(user: dict = Depends(utilisateur_courant)):
    with get_db() as db:
        societes = db.query(Societe).order_by(Societe.nom).all()
        resultat = []

        for s in societes:
            comptes_info = []
            for c in s.comptes:
                releves = db.query(Releve).filter(Releve.compte_id == c.id).order_by(Releve.annee.desc(), Releve.mois.desc()).all()
                mois_traites = [
                    {
                        "mois": r.mois, "annee": r.annee, "statut": r.statut,
                        "solde_initial": r.solde_initial, "solde_final": r.solde_final_calcule,
                    }
                    for r in releves
                ]
                nb_ecarts = sum(1 for r in releves if r.statut == "ecart")
                comptes_info.append({
                    "id": c.id, "banque": c.banque, "libelle": c.libelle, "devise": c.devise,
                    "mois_debut_exercice": c.mois_debut_exercice,
                    "nb_releves": len(releves), "nb_ecarts": nb_ecarts,
                    "dernier_releve": mois_traites[0] if mois_traites else None,
                    "mois_traites": mois_traites,
                })
            resultat.append({"id": s.id, "nom": s.nom, "comptes": comptes_info})

        nb_anomalies_ouvertes = db.query(func.count(Anomalie.id)).filter(Anomalie.resolue == False).scalar() or 0
        nb_releves_ecart = db.query(func.count(Releve.id)).filter(Releve.statut == "ecart").scalar() or 0
        nb_factures_a_corriger = db.query(func.count(Facture.id)).filter(Facture.statut == "a_corriger").scalar() or 0

        return {
            "societes": resultat,
            "alertes": {
                "anomalies_ouvertes": nb_anomalies_ouvertes,
                "releves_en_ecart": nb_releves_ecart,
                "factures_a_corriger": nb_factures_a_corriger,
            },
        }


# ── Journal des anomalies (section 11.3) ───────────────────────────────────────

class AnomaliePayload(BaseModel):
    ref_type: str = "general"
    ref_id: Optional[int] = None
    description: str


@app.get("/api/anomalies")
def get_anomalies(resolue: Optional[bool] = Query(None), user: dict = Depends(utilisateur_courant)):
    with get_db() as db:
        q = db.query(Anomalie)
        if resolue is not None:
            q = q.filter(Anomalie.resolue == resolue)
        anomalies = q.order_by(Anomalie.date_creation.desc()).all()
        return [
            {
                "id": a.id, "ref_type": a.ref_type, "ref_id": a.ref_id,
                "description": a.description, "resolue": a.resolue,
                "date_creation": a.date_creation.isoformat() if a.date_creation else None,
            }
            for a in anomalies
        ]


@app.post("/api/anomalies")
def creer_anomalie(payload: AnomaliePayload, user: dict = Depends(utilisateur_courant)):
    with get_db() as db:
        a = Anomalie(ref_type=payload.ref_type, ref_id=payload.ref_id, description=payload.description)
        db.add(a)
        db.flush()
        return {"id": a.id}


@app.patch("/api/anomalies/{anomalie_id}/resoudre")
def resoudre_anomalie(anomalie_id: int, user: dict = Depends(utilisateur_courant)):
    with get_db() as db:
        a = db.query(Anomalie).filter(Anomalie.id == anomalie_id).first()
        if not a:
            raise HTTPException(status_code=404, detail="Anomalie introuvable")
        a.resolue = True
        a.date_resolution = datetime.utcnow()
    return {"ok": True}


# ── Module B : factures achats/ventes (section 5) ──────────────────────────────
# Lecture par extraction de texte + reconnaissance de motifs (pdfplumber, même outil
# que le module relevés) — pas d'IA de vision, pas de clé API (décidé avec Anis le
# 2026-08-08 : un fournisseur/client différent à chaque facture rend un calibrage par
# tiers inutile, contrairement aux quelques banques du module relevés).

def _facture_vers_dict(f: Facture) -> dict:
    return {
        "id": f.id,
        "societe_id": f.societe_id,
        "type": f.type,
        "tiers_id": f.tiers_id,
        "tiers_intitule": f.tiers.intitule if f.tiers else None,
        "tiers_numero_compte": f.tiers.numero_compte if f.tiers else None,
        "compte_recurrent_id": f.compte_recurrent_id,
        "compte_recurrent_intitule": f.compte_recurrent.intitule if f.compte_recurrent else None,
        "compte_recurrent_numero_compte": f.compte_recurrent.numero_compte if f.compte_recurrent else None,
        "compte_id": f.compte_id,
        "compte_intitule": f.compte.intitule if f.compte else None,
        "compte_numero_compte": f.compte.numero_compte if f.compte else None,
        "taux_tva_id": f.taux_tva_id,
        "taux_tva": _taux_tva_to_dict(f.taux_tva) if f.taux_tva else None,
        "numero": f.numero,
        "date": f.date.isoformat() if f.date else None,
        "montant_ht": f.montant_ht,
        "montant_tva": f.montant_tva,
        "montant_ttc": f.montant_ttc,
        "nature_prestation": f.nature_prestation,
        "statut": f.statut,
        "fichier_source": f.fichier_source,
        "date_import": f.date_import.isoformat() if f.date_import else None,
        "date_dernier_export": f.date_dernier_export.isoformat() if f.date_dernier_export else None,
    }


async def _importer_une_facture(file: UploadFile, societe_id: int, type: str) -> dict:
    """Traite un seul fichier de facture : sauvegarde, extraction, création en base.
    Ne lève jamais d'exception pour un problème propre au fichier (mauvais format,
    échec d'extraction) — retourne un dict avec la clé 'erreur' à la place, pour ne
    pas faire échouer tout un lot d'import à cause d'un seul fichier."""
    suffixe = Path(file.filename).suffix.lower()
    if suffixe != ".pdf" and suffixe not in EXTENSIONS_IMAGE:
        return {"fichier_source": file.filename, "erreur": "La facture doit être un PDF ou une image (JPG, PNG…)"}

    contenu = await file.read()
    nom_stocke = f"{uuid.uuid4().hex}{suffixe}"
    chemin = FACTURES_DIR / nom_stocke
    chemin.write_bytes(contenu)

    avertissement = None
    champs = {"date": None, "numero": None, "montant_ht": None, "montant_tva": None, "montant_ttc": None, "sujet_detecte": None, "nom_tiers_detecte": None}
    try:
        champs = extraire_facture(chemin)
    except ExtractionError as e:
        avertissement = str(e)

    with get_db() as db:
        facture = Facture(
            societe_id=societe_id, type=type,
            numero=champs.get("numero"), date=champs.get("date"),
            montant_ht=champs.get("montant_ht"), montant_tva=champs.get("montant_tva"),
            montant_ttc=champs.get("montant_ttc"),
            nature_prestation=champs.get("sujet_detecte"),
            statut="a_corriger",
            fichier_source=file.filename, nom_fichier_stocke=nom_stocke,
        )
        db.add(facture)
        db.flush()
        resultat = _facture_vers_dict(facture)

    resultat["nom_tiers_detecte"] = champs.get("nom_tiers_detecte")
    resultat["avertissement"] = avertissement
    return resultat


@app.post("/api/factures/upload")
async def upload_facture(
    societe_id: int = Form(...),
    type: str = Form(...),   # achat | vente
    files: list[UploadFile] = File(...),
    user: dict = Depends(utilisateur_courant),
):
    if type not in ("achat", "vente"):
        raise HTTPException(status_code=400, detail="Type invalide (achat ou vente attendu)")

    with get_db() as db:
        if not db.query(Societe).filter(Societe.id == societe_id).first():
            raise HTTPException(status_code=404, detail="Société introuvable")

    return [await _importer_une_facture(file, societe_id, type) for file in files]


@app.get("/api/factures")
def get_factures(
    societe_id: int = Query(...),
    type: Optional[str] = Query(None),
    statut: Optional[str] = Query(None),
    user: dict = Depends(utilisateur_courant),
):
    with get_db() as db:
        q = db.query(Facture).filter(Facture.societe_id == societe_id)
        if type:
            q = q.filter(Facture.type == type)
        if statut:
            q = q.filter(Facture.statut == statut)
        factures = q.order_by(Facture.date_import.desc()).all()
        return [_facture_vers_dict(f) for f in factures]


@app.get("/api/factures/{facture_id}/fichier")
def get_facture_fichier(facture_id: int, user: dict = Depends(utilisateur_courant)):
    with get_db() as db:
        f = db.query(Facture).filter(Facture.id == facture_id).first()
        if not f or not f.nom_fichier_stocke:
            raise HTTPException(status_code=404, detail="Fichier introuvable")
        chemin = FACTURES_DIR / f.nom_fichier_stocke
        nom_original = f.fichier_source
    if not chemin.exists():
        raise HTTPException(status_code=404, detail="Fichier introuvable sur disque")
    return FileResponse(chemin, filename=nom_original, content_disposition_type="inline")


class FacturePayload(BaseModel):
    tiers_id: Optional[int] = None
    compte_recurrent_id: Optional[int] = None
    compte_id: Optional[int] = None
    taux_tva_id: Optional[int] = None
    numero: Optional[str] = None
    date: Optional[str] = None   # AAAA-MM-JJ
    montant_ht: Optional[float] = None
    montant_tva: Optional[float] = None
    montant_ttc: Optional[float] = None
    nature_prestation: Optional[str] = None
    statut: Optional[str] = None


@app.patch("/api/factures/{facture_id}")
def maj_facture(facture_id: int, payload: FacturePayload, user: dict = Depends(utilisateur_courant)):
    with get_db() as db:
        f = db.query(Facture).filter(Facture.id == facture_id).first()
        if not f:
            raise HTTPException(status_code=404, detail="Facture introuvable")

        # Tiers et compte d'attente sont mutuellement exclusifs (l'un remplace l'autre pour la
        # même case "fournisseur/client") — tiers_id prioritaire si les deux sont envoyés en même
        # temps (évolution du 2026-08-16).
        if payload.compte_recurrent_id is not None:
            if not db.query(ComptesRecurrents).filter(ComptesRecurrents.id == payload.compte_recurrent_id).first():
                raise HTTPException(status_code=404, detail="Compte d'attente introuvable")
            f.compte_recurrent_id = payload.compte_recurrent_id
            f.tiers_id = None
        if payload.tiers_id is not None:
            if not db.query(Tiers).filter(Tiers.id == payload.tiers_id).first():
                raise HTTPException(status_code=404, detail="Tiers introuvable")
            f.tiers_id = payload.tiers_id
            f.compte_recurrent_id = None
        if payload.compte_id is not None:
            if not db.query(Tiers).filter(Tiers.id == payload.compte_id).first():
                raise HTTPException(status_code=404, detail="Compte comptable introuvable")
            f.compte_id = payload.compte_id
        if payload.taux_tva_id is not None:
            if not db.query(TauxTVA).filter(TauxTVA.id == payload.taux_tva_id, TauxTVA.societe_id == f.societe_id).first():
                raise HTTPException(status_code=404, detail="Taux de TVA introuvable pour cette société")
            f.taux_tva_id = payload.taux_tva_id
        if payload.numero is not None:
            f.numero = payload.numero.strip() or None
        if payload.date is not None:
            try:
                f.date = datetime.strptime(payload.date, "%Y-%m-%d").date() if payload.date else None
            except ValueError:
                raise HTTPException(status_code=400, detail="Date invalide (AAAA-MM-JJ attendu)")
        if payload.montant_ht is not None:
            f.montant_ht = payload.montant_ht
        if payload.montant_tva is not None:
            f.montant_tva = payload.montant_tva
        if payload.montant_ttc is not None:
            f.montant_ttc = payload.montant_ttc
        if payload.nature_prestation is not None:
            f.nature_prestation = payload.nature_prestation.strip() or None
        if payload.statut is not None:
            if payload.statut not in ("ok", "a_corriger"):
                raise HTTPException(status_code=400, detail="Statut invalide")
            f.statut = payload.statut

        return _facture_vers_dict(f)


@app.delete("/api/factures/{facture_id}")
def supprimer_facture(facture_id: int, user: dict = Depends(utilisateur_courant)):
    with get_db() as db:
        f = db.query(Facture).filter(Facture.id == facture_id).first()
        if not f:
            raise HTTPException(status_code=404, detail="Facture introuvable")
        nom_stocke = f.nom_fichier_stocke
        db.delete(f)
    if nom_stocke:
        (FACTURES_DIR / nom_stocke).unlink(missing_ok=True)
    return {"ok": True}


# ── Export FEC des factures (évolution du 2026-08-16, demande Anis) ────────────
# Sans sélection explicite, exporte toutes les factures validées (statut=ok) de la société —
# avec sélection (facture_ids), seulement celles-ci. Marque date_dernier_export sur les factures
# incluses et consomme le compteur d'écriture partagé avec le FEC des relevés (Societe).

@app.get("/api/factures/export-fec")
def export_fec_factures(
    societe_id: int = Query(...),
    facture_ids: Optional[str] = Query(None, description="IDs séparés par des virgules, sinon toutes les factures validées (statut=ok)"),
    user: dict = Depends(utilisateur_courant),
):
    with get_db() as db:
        societe = db.query(Societe).filter(Societe.id == societe_id).first()
        if not societe:
            raise HTTPException(status_code=404, detail="Société introuvable")

        q = db.query(Facture).filter(Facture.societe_id == societe_id)
        if facture_ids:
            try:
                ids = {int(x) for x in facture_ids.split(",") if x.strip()}
            except ValueError:
                raise HTTPException(status_code=400, detail="Liste de factures invalide")
            q = q.filter(Facture.id.in_(ids))
        else:
            q = q.filter(Facture.statut == "ok")
        factures = q.order_by(Facture.date).all()
        if not factures:
            raise HTTPException(status_code=400, detail="Aucune facture sélectionnée")

        # Le compte récurrent par défaut n'est requis que si au moins une facture sélectionnée
        # n'a pas de compte de charge/produit rattaché — inutile de bloquer l'export sinon.
        if any(f.compte_id is None for f in factures):
            compte_defaut_numero, compte_defaut_libelle = _charger_compte_recurrent_defaut(db, societe_id)
        else:
            compte_defaut_numero, compte_defaut_libelle = "", ""

        numero_depart = _numero_ecriture_depart(db, societe_id)
        try:
            lignes, prochain = generer_lignes_fec_factures(
                factures, societe.journal_achats, societe.journal_ventes,
                compte_defaut_numero, compte_defaut_libelle, numero_depart,
            )
        except FECGenerationError as e:
            raise HTTPException(status_code=400, detail=str(e))

        buf = exporter_fec_xlsx(lignes)
        nom = f"Export_factures_{societe.nom}_{datetime.utcnow().strftime('%Y%m%d')}.xlsx".replace(" ", "-")
        maintenant = datetime.utcnow()
        for f in factures:
            f.date_dernier_export = maintenant
        _persister_numero_ecriture(db, societe_id, prochain)

    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nom}"'},
    )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8003, reload=True)

