"""
Autenticazione — tre ruoli:
  - admin:   accesso alla sezione riservata (gestione catalogo, alunni, classi, prof)
  - student: accesso minimo, rilasciato dopo verifica nome+PIN, usato per
             autorizzare le operazioni di prestito/restituzione lato server.
  - teacher: accesso al portale notifiche, rilasciato dopo login con email
             e password (scelta dal professore al primo accesso).

Variabili d'ambiente richieste (NESSUN valore di default insicuro: se mancano,
l'app si rifiuta di avviarsi):
  BIBLIOTECA_SECRET      stringa casuale lunga (es. openssl rand -hex 32)
  BIBLIOTECA_ADMIN_HASH  hash bcrypt della password admin

Generare l'hash:
  python -c "import bcrypt; print(bcrypt.hashpw(b'tuapassword', bcrypt.gensalt()).decode())"
oppure: python setup_admin.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt

SECRET_KEY = os.environ.get("BIBLIOTECA_SECRET", "").strip()
ADMIN_HASH = os.environ.get("BIBLIOTECA_ADMIN_HASH", "").strip()
ALGORITHM  = "HS256"

ADMIN_TOKEN_EXPIRE   = 480  # minuti (8 ore) — sessione admin
STUDENT_TOKEN_EXPIRE = 30   # minuti — sessione studente, breve apposta:
                             # serve solo per il tempo di usare il chiosco
TEACHER_TOKEN_EXPIRE = 480  # minuti (8 ore) — sessione professore, come admin:
                             # accede da un dispositivo proprio, non condiviso

# Compatibilità con il vecchio nome usato altrove nel codice/storico
TOKEN_EXPIRE = ADMIN_TOKEN_EXPIRE

# ── Fail fast: niente più password/secret di default in chiaro nel codice ────
# Se le variabili non sono impostate, l'app si rifiuta di partire invece di
# "declassare" silenziosamente a una password nota scritta nel codice.
if not SECRET_KEY:
    print(
        "\n[ERRORE FATALE] Variabile BIBLIOTECA_SECRET non impostata.\n"
        "Genera un secret con: python -c \"import secrets; print(secrets.token_hex(32))\"\n"
        "e impostalo come variabile d'ambiente (o nel file .env in locale).\n",
        file=sys.stderr,
    )
    sys.exit(1)

if not ADMIN_HASH:
    print(
        "\n[ERRORE FATALE] Variabile BIBLIOTECA_ADMIN_HASH non impostata.\n"
        "Genera l'hash della password admin con: python setup_admin.py\n",
        file=sys.stderr,
    )
    sys.exit(1)

bearer_scheme = HTTPBearer(auto_error=False)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


def create_access_token(data: dict, expires_minutes: int) -> str:
    payload = dict(data)
    payload["exp"] = datetime.now(timezone.utc) + timedelta(minutes=expires_minutes)
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def create_admin_token() -> str:
    return create_access_token({"role": "admin"}, ADMIN_TOKEN_EXPIRE)


def create_student_token(full_name: str) -> str:
    return create_access_token({"role": "student", "name": full_name}, STUDENT_TOKEN_EXPIRE)


def create_teacher_token(teacher_id: str) -> str:
    return create_access_token({"role": "teacher", "teacher_id": teacher_id}, TEACHER_TOKEN_EXPIRE)


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


def require_admin(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
):
    """Dependency: richiede token admin valido."""
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token mancante",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_token(credentials.credentials)
    if not payload or payload.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token non valido o scaduto",
        )
    return payload


def require_student(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> dict:
    """
    Dependency: richiede un token studente valido (rilasciato da /auth/student-login
    o /auth/login-noauth quando il PIN è disabilitato). Restituisce il payload con
    il nome dello studente verificato lato server — l'endpoint di prestito userà
    SEMPRE questo nome, non quello passato liberamente dal client.
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sessione mancante: effettua di nuovo il login.",
        )
    payload = decode_token(credentials.credentials)
    if not payload or payload.get("role") != "student":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sessione non valida o scaduta: effettua di nuovo il login.",
        )
    return payload


def require_teacher(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> dict:
    """
    Dependency: richiede un token professore valido (rilasciato da
    /auth/teacher-login). Restituisce il payload con teacher_id verificato
    lato server.
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sessione mancante: effettua di nuovo il login.",
        )
    payload = decode_token(credentials.credentials)
    if not payload or payload.get("role") != "teacher":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sessione non valida o scaduta: effettua di nuovo il login.",
        )
    return payload
