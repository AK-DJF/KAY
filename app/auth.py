# auth.py
# Authentification minimale par session (section 7 du cadrage) :
# compte/mot de passe, modèle prêt à associer un utilisateur à ses actions,
# rôles admin/collaborateur posés dès la v1 (portée des droits affinée plus tard).

import bcrypt
from fastapi import HTTPException, Request

from database import User, get_db


def hasher_mot_de_passe(mot_de_passe: str) -> str:
    return bcrypt.hashpw(mot_de_passe.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verifier_mot_de_passe(mot_de_passe: str, hash_stocke: str) -> bool:
    try:
        return bcrypt.checkpw(mot_de_passe.encode("utf-8"), hash_stocke.encode("utf-8"))
    except ValueError:
        return False


def aucun_utilisateur_existant() -> bool:
    with get_db() as db:
        return db.query(User).count() == 0


def utilisateur_courant(request: Request) -> dict:
    """Dépendance FastAPI : lève 401 si la session n'est pas authentifiée."""
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Non authentifié")

    with get_db() as db:
        user = db.query(User).filter(User.id == user_id, User.actif == True).first()
        if not user:
            raise HTTPException(status_code=401, detail="Session invalide")
        return {"id": user.id, "email": user.email, "role": user.role}


def exiger_admin(user: dict) -> None:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Réservé aux administrateurs")
