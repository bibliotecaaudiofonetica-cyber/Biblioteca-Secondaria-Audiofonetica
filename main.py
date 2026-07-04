"""
Biblioteca Scolastica — FastAPI Backend
Avvio locale: uvicorn main:app --reload --port 8000
Avvio produzione: uvicorn main:app --host 0.0.0.0 --port $PORT
"""

import os
import re
import uuid
import threading
import logging
import httpx
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import or_, text, func
from sqlalchemy.orm import Session
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

import auth
import models
import genre_taxonomy
import badges
import email_service
from database import Base, engine, get_db, is_postgres

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("biblioteca")

# ── Crea tabelle ────────────────────────────────────────────────────────────
Base.metadata.create_all(bind=engine)

# ── Migrazioni runtime (colonne/tabelle aggiunte dopo la creazione iniziale) ──
# Nota: create_all() crea già le tabelle NUOVE (classes, teachers, teacher_classes)
# su un database vuoto. Queste ALTER TABLE servono solo per i database PREESISTENTI
# (es. la tua installazione attuale) che non hanno ancora le colonne nuove.
def _run_migrations():
    if is_postgres():
        migrations = [
            "ALTER TABLE books ADD COLUMN IF NOT EXISTS cover_url TEXT",
            "ALTER TABLE books ADD COLUMN IF NOT EXISTS openlibrary_checked BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS notes TEXT DEFAULT ''",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS email TEXT",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS class_id TEXT",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS score INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS score_year TEXT",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMPTZ",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS last_inactivity_penalty_at TIMESTAMPTZ",
            "ALTER TABLE loans ADD COLUMN IF NOT EXISTS last_student_reminder_at TIMESTAMPTZ",
            "ALTER TABLE loans ADD COLUMN IF NOT EXISTS last_teacher_alert_at TIMESTAMPTZ",
            "ALTER TABLE loans ADD COLUMN IF NOT EXISTS teacher_alert_stage INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE teachers ADD COLUMN IF NOT EXISTS password_hash TEXT",
            "ALTER TABLE teachers ADD COLUMN IF NOT EXISTS full_name TEXT",
            "ALTER TABLE teachers ADD COLUMN IF NOT EXISTS email TEXT",
            "ALTER TABLE class_recommendations ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ",
        ]
    else:
        # SQLite non supporta "IF NOT EXISTS" su ADD COLUMN: si usa try/except.
        migrations = [
            "ALTER TABLE books ADD COLUMN cover_url TEXT",
            "ALTER TABLE books ADD COLUMN openlibrary_checked INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE students ADD COLUMN notes TEXT DEFAULT ''",
            "ALTER TABLE students ADD COLUMN email TEXT",
            "ALTER TABLE students ADD COLUMN class_id TEXT",
            "ALTER TABLE students ADD COLUMN score INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE students ADD COLUMN score_year TEXT",
            "ALTER TABLE students ADD COLUMN last_activity_at TIMESTAMP",
            "ALTER TABLE students ADD COLUMN last_inactivity_penalty_at TIMESTAMP",
            "ALTER TABLE loans ADD COLUMN last_student_reminder_at TIMESTAMP",
            "ALTER TABLE loans ADD COLUMN last_teacher_alert_at TIMESTAMP",
            "ALTER TABLE loans ADD COLUMN teacher_alert_stage INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE teachers ADD COLUMN password_hash TEXT",
            "ALTER TABLE teachers ADD COLUMN full_name TEXT",
            "ALTER TABLE teachers ADD COLUMN email TEXT",
            "ALTER TABLE class_recommendations ADD COLUMN expires_at TIMESTAMP",
        ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                conn.rollback()  # colonna già presente, oppure sintassi non supportata — ignorato

    # Backfill: i professori creati con una versione precedente del sistema
    # (basata su email) potrebbero non avere ancora full_name popolato.
    # Lo deriviamo da first_name + last_name, una volta sola.
    try:
        with engine.connect() as conn:
            conn.execute(text(
                "UPDATE teachers SET full_name = first_name || ' ' || last_name "
                "WHERE full_name IS NULL"
            ))
            conn.commit()
    except Exception:
        pass  # tabella vuota o altra incompatibilità minore, non bloccante

_run_migrations()

limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title="Biblioteca Scolastica API",
    version="3.0.0",
    description="Backend per la gestione dei prestiti della biblioteca scolastica",
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS: in produzione si può restringere con la variabile CORS_ORIGINS
# (lista separata da virgole, es. "https://biblioteca-scuola.pages.dev").
# Se non impostata, resta apertura totale (comodo in fase di sviluppo/test).
_cors_origins_env = os.environ.get("CORS_ORIGINS", "").strip()
_cors_origins = [o.strip() for o in _cors_origins_env.split(",") if o.strip()] or ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


# ══════════════════════════════════════════════════════════════════════════════
# Pydantic schemas
# ══════════════════════════════════════════════════════════════════════════════

class LoginRequest(BaseModel):
    password: str


class BookCreate(BaseModel):
    title: str
    author: str
    publisher: str = ""
    location: str
    genre: str = ""


class BookUpdate(BaseModel):
    title:     Optional[str] = None
    author:    Optional[str] = None
    publisher: Optional[str] = None
    location:  Optional[str] = None
    genre:     Optional[str] = None
    cover_url: Optional[str] = None


class LoanCreate(BaseModel):
    book_id: str
    days: int = 7


class LoanExtend(BaseModel):
    days: int = 7


class WaitlistToggle(BaseModel):
    user: str


class BulkImport(BaseModel):
    books: list[BookCreate]


class BackupData(BaseModel):
    books: list[dict]
    loans: list[dict]
    waitlist: list[dict]


class StudentLogin(BaseModel):
    full_name: str
    pin: str


class StudentForgotPin(BaseModel):
    full_name: str


class TeacherLogin(BaseModel):
    full_name: str
    password: str


class TeacherSetPassword(BaseModel):
    """Usata al primo accesso, quando il professore non ha ancora una password."""
    full_name: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def validate_password_strength(cls, v):
        if len(v) < 6:
            raise ValueError("La password deve avere almeno 6 caratteri")
        return v


class StudentCreate(BaseModel):
    full_name: str
    notes: str = ""
    class_id: str


class StudentUpdate(BaseModel):
    notes:     Optional[str] = None
    active:    Optional[bool] = None
    class_id:  Optional[str] = None
    email:     Optional[str] = None
    full_name: Optional[str] = None


class StudentEmailUpdate(BaseModel):
    """Usata dallo studente per impostare/aggiornare la propria email (al primo login o dal profilo)."""
    email: str


class TeacherEmailUpdate(BaseModel):
    """Usata dal professore per impostare la propria email al primo accesso."""
    email: str


class ManualReminderBody(BaseModel):
    """Body opzionale per promemoria manuale (non usato ora, ma utile per future personalizzazioni)."""
    pass


class ClassCreate(BaseModel):
    name: str
    school_year: str = ""


class ClassUpdate(BaseModel):
    name:        Optional[str] = None
    school_year: Optional[str] = None


class TeacherCreate(BaseModel):
    first_name: str
    last_name: str
    class_ids: list[str] = []


class TeacherUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name:  Optional[str] = None
    active:     Optional[bool] = None
    class_ids:  Optional[list[str]] = None


class ReviewCreate(BaseModel):
    book_id: str
    rating: int
    text: str = ""
    loan_id: Optional[str] = None

    @field_validator("rating")
    @classmethod
    def validate_rating(cls, v):
        if v < 1 or v > 5:
            raise ValueError("Il voto deve essere tra 1 e 5")
        return v


class ClassRecommendationCreate(BaseModel):
    class_id: str
    category: str
    subcategory: Optional[str] = None
    note: str = ""


class BroadcastMessage(BaseModel):
    """
    Messaggio personalizzato inviato dall'admin a un gruppo di destinatari.
    target: "all_students" | "all_teachers" | "classes" | "students"
    class_ids / student_ids sono usati solo quando target è rispettivamente
    "classes" o "students".
    """
    target: str
    class_ids: list[str] = []
    student_ids: list[str] = []
    subject: str
    message: str

    @field_validator("target")
    @classmethod
    def validate_target(cls, v):
        allowed = {"all_students", "all_teachers", "classes", "students"}
        if v not in allowed:
            raise ValueError(f"target deve essere uno di: {', '.join(sorted(allowed))}")
        return v

    @field_validator("message")
    @classmethod
    def validate_message(cls, v):
        if not v or not v.strip():
            raise ValueError("Il messaggio non può essere vuoto")
        return v


# ══════════════════════════════════════════════════════════════════════════════
# AUTH
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/auth/login", tags=["Auth"])
@limiter.limit("10/minute")
def login(body: LoginRequest, request: Request):
    """Verifica password admin e restituisce JWT. Limitata a 10 tentativi/minuto per IP."""
    if not auth.verify_password(body.password, auth.ADMIN_HASH):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Password errata")

    token = auth.create_admin_token()
    return {"token": token, "expires_in": auth.ADMIN_TOKEN_EXPIRE * 60}


@app.post("/auth/student-login", tags=["Auth"])
@limiter.limit("20/minute")
def student_login(body: StudentLogin, request: Request, db: Session = Depends(get_db)):
    """
    Verifica nome+PIN quando pin_auth_enabled=true. Limitata a 20 tentativi/minuto per IP
    (un PIN a 5 cifre è altrimenti forzabile facilmente senza questo limite).
    Restituisce un token studente di breve durata, usato per autorizzare i prestiti.
    """
    pin_enabled = get_setting(db, SETTING_PIN_AUTH) == "true"

    if not pin_enabled:
        # Funzionalità disabilitata: accesso libero, basta che il nome corrisponda
        # a un alunno esistente e attivo (comunque verificato, per evitare nomi a caso).
        student = db.query(models.Student).filter_by(full_name=body.full_name).first()
        if not student:
            raise HTTPException(403, "STUDENT_NOT_FOUND")
        if not student.active:
            raise HTTPException(403, "STUDENT_INACTIVE")
        token = auth.create_student_token(student.full_name)
        return {"ok": True, "fullName": student.full_name, "token": token}

    student = db.query(models.Student).filter_by(full_name=body.full_name).first()
    if not student:
        raise HTTPException(403, "STUDENT_NOT_FOUND")
    if not student.active:
        raise HTTPException(403, "STUDENT_INACTIVE")
    if student.pin != body.pin.strip():
        raise HTTPException(401, "WRONG_PIN")

    token = auth.create_student_token(student.full_name)
    return {"ok": True, "fullName": student.full_name, "token": token}


@app.post("/auth/teacher-login", tags=["Auth"])
@limiter.limit("10/minute")
def teacher_login(body: TeacherLogin, request: Request, db: Session = Depends(get_db)):
    """
    Login professore con nome+cognome + password. Se il professore non ha
    ancora impostato una password (primo accesso), restituisce
    needsPassword=true e il frontend deve chiamare /auth/teacher-set-password
    invece di questo. Limitata a 10 tentativi/minuto per IP.
    """
    teacher = db.query(models.Teacher).filter_by(full_name=body.full_name).first()
    if not teacher or not teacher.active:
        raise HTTPException(403, "TEACHER_NOT_FOUND")

    if not teacher.password_hash:
        return {"needsPassword": True}

    if not auth.verify_password(body.password, teacher.password_hash):
        raise HTTPException(401, "WRONG_PASSWORD")

    token = auth.create_teacher_token(teacher.id)
    return {
        "ok": True, "token": token, "needsPassword": False,
        "teacher": teacher.to_dict(),
        "expires_in": auth.TEACHER_TOKEN_EXPIRE * 60,
    }


@app.post("/auth/teacher-set-password", tags=["Auth"])
@limiter.limit("10/minute")
def teacher_set_password(body: TeacherSetPassword, request: Request, db: Session = Depends(get_db)):
    """
    Imposta la password al primo accesso. Funziona SOLO se il professore non
    ha ancora una password impostata (altrimenti va usato il login normale);
    questo evita che chiunque possa "rubare" un account semplicemente
    conoscendo nome e cognome di un professore già attivo.
    """
    import bcrypt as bcrypt_module
    teacher = db.query(models.Teacher).filter_by(full_name=body.full_name).first()
    if not teacher or not teacher.active:
        raise HTTPException(403, "TEACHER_NOT_FOUND")
    if teacher.password_hash:
        raise HTTPException(400, "PASSWORD_ALREADY_SET")

    hashed = bcrypt_module.hashpw(body.new_password.encode(), bcrypt_module.gensalt()).decode()
    teacher.password_hash = hashed
    db.commit()

    token = auth.create_teacher_token(teacher.id)
    return {
        "ok": True, "token": token,
        "teacher": teacher.to_dict(),
        "expires_in": auth.TEACHER_TOKEN_EXPIRE * 60,
    }


# ══════════════════════════════════════════════════════════════════════════════
# RECUPERO PASSWORD DOCENTE
# ══════════════════════════════════════════════════════════════════════════════

# Store in-memory dei token di reset (reset al riavvio del server — accettabile
# per un sistema scolastico; per produzione robusta usare una tabella DB).
import secrets as _secrets_mod
import time as _time_mod
_reset_tokens: dict = {}   # token -> {teacher_id, expires}
RESET_TOKEN_TTL = 3600     # secondi (1 ora)

FRONTEND_URL = os.environ.get("FRONTEND_URL", "").strip().rstrip("/")


class TeacherForgotPassword(BaseModel):
    full_name: str


class TeacherResetPassword(BaseModel):
    token: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def validate_pwd(cls, v):
        if len(v) < 6:
            raise ValueError("La password deve avere almeno 6 caratteri")
        return v


@app.post("/auth/teacher-forgot-password", tags=["Auth"])
@limiter.limit("5/minute")
def teacher_forgot_password(body: TeacherForgotPassword, request: Request,
                             db: Session = Depends(get_db)):
    """
    Invia email con link di reset password al professore, se ha un'email registrata.
    Restituisce sempre 200 per non rivelare se il nome esiste (security best practice),
    tranne se l'account non ha email (errore esplicito per UX).
    """
    teacher = db.query(models.Teacher).filter_by(
        full_name=body.full_name.strip(), active=True
    ).first()
    if not teacher:
        raise HTTPException(404, "TEACHER_NOT_FOUND")
    if not teacher.email:
        raise HTTPException(400, "NO_EMAIL")

    # Genera token sicuro
    token = _secrets_mod.token_urlsafe(32)
    _reset_tokens[token] = {
        "teacher_id": teacher.id,
        "expires": _time_mod.time() + RESET_TOKEN_TTL,
    }

    reset_url = f"{FRONTEND_URL or ''}/?reset_token={token}"
    html = email_service.build_reset_password_html(teacher.full_name, reset_url)
    email_service.send_email(
        to_email=teacher.email,
        to_name=teacher.full_name,
        subject="🔑 Reimposta la tua password — Biblioteca Scolastica",
        html_body=html,
    )
    logger.info("Reset password richiesto per: %s", teacher.full_name)
    return {"ok": True}


@app.post("/auth/student-forgot-pin", tags=["Auth"])
@limiter.limit("5/minute")
def student_forgot_pin(body: StudentForgotPin, request: Request,
                        db: Session = Depends(get_db)):
    """
    "PIN dimenticato" per gli alunni: a differenza dei professori, lo
    studente NON può reimpostare il PIN da solo. Questa richiesta:
      1) crea sempre una notifica interna per l'admin, che dovrà generare
         un nuovo PIN e consegnarlo di persona;
      2) se l'alunno ha un'email registrata, gli invia una email di
         conferma — SENZA il PIN, che infatti non esiste ancora a questo
         punto (verrà scelto/generato dall'admin in un secondo momento).
    """
    student = db.query(models.Student).filter_by(
        full_name=body.full_name.strip(), active=True
    ).first()
    if not student:
        raise HTTPException(404, "STUDENT_NOT_FOUND")

    class_name = student.school_class.name if student.school_class else "nessuna classe"
    create_notification(
        db, "admin", "admin", "pin_reset_request",
        title=f"🔑 Richiesta reset PIN: {student.full_name}",
        body=f"Classe: {class_name}. Genera un nuovo PIN e consegnalo di persona all'alunno.",
        related_loan_id=student.id,
    )

    email_sent = False
    if student.email:
        try:
            email_sent = email_service.send_pin_reset_request(student.full_name, student.email)
        except Exception:
            logger.exception("Errore invio email richiesta reset PIN per %s", student.full_name)

    logger.info("Richiesta reset PIN per: %s", student.full_name)
    return {"ok": True, "hasEmail": bool(student.email), "emailSent": email_sent}


@app.get("/admin/pin-reset-requests", tags=["Alunni"], dependencies=[Depends(auth.require_admin)])
def list_pin_reset_requests(db: Session = Depends(get_db)):
    """Richieste di reset PIN in attesa (create dal pulsante "PIN dimenticato" degli alunni)."""
    notifs = (
        db.query(models.Notification)
        .filter_by(recipient_type="admin", kind="pin_reset_request", read=False)
        .order_by(models.Notification.created_at.desc())
        .all()
    )
    return [
        {
            "id": n.id,
            "studentId": n.related_loan_id,
            "title": n.title,
            "body": n.body,
            "createdAt": n.created_at.isoformat() if n.created_at else None,
        }
        for n in notifs
    ]


@app.post("/admin/pin-reset-requests/{notification_id}/dismiss", tags=["Alunni"],
          dependencies=[Depends(auth.require_admin)])
def dismiss_pin_reset_request(notification_id: str, db: Session = Depends(get_db)):
    """Segna come gestita una richiesta di reset PIN (non tocca il PIN in sé)."""
    notif = db.query(models.Notification).filter_by(
        id=notification_id, recipient_type="admin", kind="pin_reset_request",
    ).first()
    if not notif:
        raise HTTPException(404, "Richiesta non trovata")
    notif.read = True
    db.commit()
    return {"ok": True}


@app.post("/auth/teacher-reset-password", tags=["Auth"])
def teacher_reset_password(body: TeacherResetPassword, db: Session = Depends(get_db)):
    """Reimposta la password usando il token ricevuto via email."""
    import bcrypt as bcrypt_module
    entry = _reset_tokens.get(body.token)
    if not entry:
        raise HTTPException(400, "Token non valido o già usato")
    if _time_mod.time() > entry["expires"]:
        _reset_tokens.pop(body.token, None)
        raise HTTPException(400, "Token scaduto — richiedi un nuovo link")

    teacher = db.query(models.Teacher).get(entry["teacher_id"])
    if not teacher or not teacher.active:
        raise HTTPException(404, "Professore non trovato")

    hashed = bcrypt_module.hashpw(body.new_password.encode(), bcrypt_module.gensalt()).decode()
    teacher.password_hash = hashed
    db.commit()
    _reset_tokens.pop(body.token, None)   # token monouso
    return {"ok": True}

@app.patch("/students/me/email", tags=["Auth"])
def student_set_email(body: StudentEmailUpdate, db: Session = Depends(get_db),
                      student_payload: dict = Depends(auth.require_student)):
    """Imposta o aggiorna l'email dello studente loggato (facoltativo)."""
    student = db.query(models.Student).filter_by(full_name=student_payload["name"]).first()
    if not student:
        raise HTTPException(404, "Alunno non trovato")
    email = body.email.strip().lower() if body.email else None
    student.email = email
    db.commit()
    return {"ok": True, "email": student.email, "hasEmail": bool(student.email)}


@app.get("/students/me/email-status", tags=["Auth"])
def student_email_status(db: Session = Depends(get_db),
                         student_payload: dict = Depends(auth.require_student)):
    """Restituisce se lo studente loggato ha già un'email impostata."""
    student = db.query(models.Student).filter_by(full_name=student_payload["name"]).first()
    if not student:
        raise HTTPException(404, "Alunno non trovato")
    return {"hasEmail": bool(student.email), "email": student.email or ""}


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL PROFESSORE — imposta al primo accesso o aggiorna dal portale
# ══════════════════════════════════════════════════════════════════════════════

@app.patch("/teacher/me/email", tags=["Portale Professori"])
def teacher_set_email(body: TeacherEmailUpdate, db: Session = Depends(get_db),
                      teacher_payload: dict = Depends(auth.require_teacher)):
    """Imposta o aggiorna l'email del professore loggato."""
    teacher = db.query(models.Teacher).get(teacher_payload["teacher_id"])
    if not teacher:
        raise HTTPException(404, "Professore non trovato")
    email = body.email.strip().lower() if body.email else None
    teacher.email = email
    db.commit()
    return {"ok": True, "email": teacher.email}


# ══════════════════════════════════════════════════════════════════════════════
# PROMEMORIA MANUALE — admin e professore
# ══════════════════════════════════════════════════════════════════════════════

def _send_manual_reminder_for_loan(loan: "models.Loan", db: "Session") -> dict:
    """
    Invia notifica interna + email (se disponibile) per un singolo prestito in ritardo.
    Restituisce un dict con il risultato dell'invio.
    """
    from datetime import datetime, timezone as tz
    now = datetime.now(tz.utc)
    days_late = max(0, (now - loan.due_date).days)
    due_str = loan.due_date.strftime("%d/%m/%Y")
    giorni = "giorno" if days_late == 1 else "giorni"

    # Notifica interna
    notif = models.Notification(
        recipient_type="student",
        recipient_id=loan.user,
        kind="overdue",
        title=f"📕 Promemoria: {loan.book_title}",
        body=f"Scadenza {due_str} — in ritardo di {days_late} {giorni}. Riportalo appena possibile.",
        related_loan_id=loan.id,
    )
    db.add(notif)
    db.commit()

    # Email (se lo studente ha un'email)
    student = db.query(models.Student).filter_by(full_name=loan.user).first()
    email_sent = False
    if student and student.email:
        email_sent = email_service.send_overdue_student(
            student_name=student.full_name,
            student_email=student.email,
            book_title=loan.book_title,
            due_date_str=due_str,
            days_late=days_late,
        )

    return {
        "loanId": loan.id,
        "user": loan.user,
        "bookTitle": loan.book_title,
        "notificationSent": True,
        "emailSent": email_sent,
        "hasEmail": bool(student and student.email),
    }


@app.post("/admin/send-reminder/{loan_id}", tags=["Setup"],
          dependencies=[Depends(auth.require_admin)])
def admin_send_reminder(loan_id: str, db: Session = Depends(get_db)):
    """Invia promemoria manuale (notifica interna + email) per un singolo prestito in ritardo."""
    loan = db.query(models.Loan).get(loan_id)
    if not loan or loan.returned:
        raise HTTPException(404, "Prestito non trovato o già restituito")
    result = _send_manual_reminder_for_loan(loan, db)
    return {"ok": True, "result": result}


@app.post("/admin/send-reminder-all", tags=["Setup"],
          dependencies=[Depends(auth.require_admin)])
def admin_send_reminder_all(db: Session = Depends(get_db)):
    """Invia promemoria manuale a TUTTI gli alunni con prestiti scaduti."""
    from datetime import datetime, timezone as tz
    now = datetime.now(tz.utc)
    overdue = db.query(models.Loan).filter(
        models.Loan.returned == False,
        models.Loan.due_date < now,
    ).all()
    results = [_send_manual_reminder_for_loan(l, db) for l in overdue]
    return {
        "ok": True,
        "total": len(results),
        "emailSent": sum(1 for r in results if r["emailSent"]),
        "results": results,
    }


@app.post("/admin/broadcast", tags=["Messaggi"], dependencies=[Depends(auth.require_admin)])
def admin_broadcast(body: BroadcastMessage, db: Session = Depends(get_db)):
    """
    Invia un messaggio personalizzato a un gruppo di destinatari scelto
    dall'admin: tutti gli alunni, tutti i professori, gli alunni di alcune
    classi, oppure alunni specifici. Ogni destinatario riceve sempre una
    notifica interna (visibile nel portale); riceve anche un'email se ha
    un indirizzo email impostato.
    """
    students: list["models.Student"] = []
    teachers: list["models.Teacher"] = []

    if body.target == "all_students":
        students = db.query(models.Student).filter_by(active=True).all()
    elif body.target == "all_teachers":
        teachers = db.query(models.Teacher).filter_by(active=True).all()
    elif body.target == "classes":
        if not body.class_ids:
            raise HTTPException(400, "Seleziona almeno una classe")
        students = (
            db.query(models.Student)
            .filter(models.Student.class_id.in_(body.class_ids), models.Student.active == True)
            .all()
        )
    elif body.target == "students":
        if not body.student_ids:
            raise HTTPException(400, "Seleziona almeno un alunno")
        students = (
            db.query(models.Student)
            .filter(models.Student.id.in_(body.student_ids))
            .all()
        )

    if not students and not teachers:
        raise HTTPException(400, "Nessun destinatario trovato per questa selezione")

    students_notified = 0
    teachers_notified = 0
    emails_sent = 0

    for s in students:
        create_notification(
            db, "student", s.full_name, "admin_broadcast",
            title=f"📢 {body.subject}", body=body.message,
        )
        students_notified += 1
        if s.email:
            try:
                if email_service.send_broadcast(s.full_name, s.email, body.subject, body.message):
                    emails_sent += 1
            except Exception:
                logger.exception("Errore invio email broadcast a studente %s", s.full_name)

    for t in teachers:
        create_notification(
            db, "teacher", t.id, "admin_broadcast",
            title=f"📢 {body.subject}", body=body.message,
        )
        teachers_notified += 1
        if t.email:
            try:
                if email_service.send_broadcast(t.full_name, t.email, body.subject, body.message):
                    emails_sent += 1
            except Exception:
                logger.exception("Errore invio email broadcast a professore %s", t.full_name)

    return {
        "ok": True,
        "studentsNotified": students_notified,
        "teachersNotified": teachers_notified,
        "emailsSent": emails_sent,
    }


@app.post("/teacher/send-reminder/{loan_id}", tags=["Portale Professori"])
def teacher_send_reminder(loan_id: str, db: Session = Depends(get_db),
                          teacher_payload: dict = Depends(auth.require_teacher)):
    """
    Invia promemoria manuale per un singolo prestito in ritardo di un alunno
    della classe del professore loggato.
    """
    loan = db.query(models.Loan).get(loan_id)
    if not loan or loan.returned:
        raise HTTPException(404, "Prestito non trovato o già restituito")
    # Verifica che l'alunno appartenga a una classe del professore
    teacher = db.query(models.Teacher).get(teacher_payload["teacher_id"])
    if not teacher:
        raise HTTPException(404, "Professore non trovato")
    student = db.query(models.Student).filter_by(full_name=loan.user).first()
    if not student:
        raise HTTPException(404, "Alunno non trovato")
    class_ids = {c.id for c in teacher.classes}
    if student.class_id not in class_ids:
        raise HTTPException(403, "Questo alunno non appartiene alle tue classi")
    result = _send_manual_reminder_for_loan(loan, db)
    return {"ok": True, "result": result}


@app.post("/teacher/send-reminder-all", tags=["Portale Professori"])
def teacher_send_reminder_all(db: Session = Depends(get_db),
                              teacher_payload: dict = Depends(auth.require_teacher)):
    """Invia promemoria manuale a tutti gli alunni in ritardo nelle classi del professore loggato."""
    from datetime import datetime, timezone as tz
    now = datetime.now(tz.utc)
    teacher = db.query(models.Teacher).get(teacher_payload["teacher_id"])
    if not teacher:
        raise HTTPException(404, "Professore non trovato")
    class_ids = {c.id for c in teacher.classes}
    overdue = db.query(models.Loan).filter(
        models.Loan.returned == False,
        models.Loan.due_date < now,
    ).all()
    # Filtra solo gli alunni nelle classi del professore
    results = []
    for loan in overdue:
        student = db.query(models.Student).filter_by(full_name=loan.user).first()
        if student and student.class_id in class_ids:
            results.append(_send_manual_reminder_for_loan(loan, db))
    return {
        "ok": True,
        "total": len(results),
        "emailSent": sum(1 for r in results if r["emailSent"]),
        "results": results,
    }


# ══════════════════════════════════════════════════════════════════════════════
# BOOKS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/books", tags=["Libri"])
def list_books(
    q: str = Query("", description="Cerca titolo o autore"),
    genre: str = Query("", description="Filtra per genere (testo libero, vecchio sistema)"),
    category: str = Query("", description="Filtra per categoria della taxonomy (es. 'Avventura')"),
    subcategory: str = Query("", description="Filtra per sottocategoria della taxonomy"),
    db: Session = Depends(get_db),
):
    """Lista libri con ricerca e filtri. Restituisce anche la disponibilità."""
    query = db.query(models.Book)
    if q:
        query = query.filter(
            or_(
                models.Book.title.ilike(f"%{q}%"),
                models.Book.author.ilike(f"%{q}%"),
            )
        )
    if genre:
        query = query.filter(models.Book.genre.ilike(f"%{genre}%"))

    books = query.order_by(models.Book.title).all()

    # Filtro per categoria/sottocategoria della taxonomy: applicato in Python
    # (non in SQL) perché la classificazione si basa su parole chiave, non su
    # un campo diretto del database.
    if category:
        filtered = []
        for b in books:
            matches = genre_taxonomy.classify_genre(b.genre)
            cats = {c for c, _ in matches}
            subcats = {s for _, s in matches}
            if category not in cats:
                continue
            if subcategory and subcategory not in subcats:
                continue
            filtered.append(b)
        books = filtered

    taken_ids = {
        l.book_id
        for l in db.query(models.Loan.book_id).filter_by(returned=False).all()
    }
    result = []
    for b in books:
        d = b.to_dict()
        d["available"] = b.id not in taken_ids
        result.append(d)
    return result


@app.get("/books/genres", tags=["Libri"])
def list_genres(db: Session = Depends(get_db)):
    """Lista di tutti i generi presenti nel catalogo (testo libero, vecchio sistema)."""
    books = db.query(models.Book.genre).distinct().all()
    genres = set()
    for (g,) in books:
        if g:
            for part in g.split("/"):
                genres.add(part.strip().lower())
    return sorted(genres)


@app.get("/books/categories", tags=["Libri"])
def list_categories():
    """
    Restituisce l'albero categoria -> sottocategorie della taxonomy curata,
    usato dal frontend per la navigazione a pulsanti pensata per i ragazzi.
    """
    return genre_taxonomy.get_taxonomy_tree()


@app.get("/books/popularity", tags=["Libri"])
def books_popularity(db: Session = Depends(get_db)):
    """
    Endpoint pubblico (nessuna autenticazione): restituisce, per ogni libro
    preso in prestito almeno una volta, quante volte è stato preso in totale
    (prestiti attivi + restituiti). Nessun dato personale (niente nomi/date).

    Usato dal frontend per ordinare i libri per popolarità nella schermata
    di ricerca alunni (i più presi in cima) e dal chatbot "Consigliami un
    libro" per suggerire i titoli più richiesti.
    """
    rows = (
        db.query(models.Loan.book_id, func.count(models.Loan.id))
        .group_by(models.Loan.book_id)
        .all()
    )
    return {book_id: count for book_id, count in rows}


@app.post("/books", tags=["Libri"], dependencies=[Depends(auth.require_admin)])
def add_book(book: BookCreate, db: Session = Depends(get_db)):
    """Aggiunge un libro al catalogo (richiede admin)."""
    b = models.Book(**book.model_dump())
    db.add(b)
    db.commit()
    db.refresh(b)
    return b.to_dict()


@app.put("/books/{book_id}", tags=["Libri"], dependencies=[Depends(auth.require_admin)])
def update_book(book_id: str, data: BookUpdate, db: Session = Depends(get_db)):
    """Modifica un libro (richiede admin)."""
    book = db.query(models.Book).get(book_id)
    if not book:
        raise HTTPException(404, "Libro non trovato")
    for field, value in data.model_dump(exclude_none=True).items():
        setattr(book, field, value)
    db.commit()
    db.refresh(book)
    return book.to_dict()


@app.delete("/books/{book_id}", tags=["Libri"], dependencies=[Depends(auth.require_admin)])
def delete_book(book_id: str, db: Session = Depends(get_db)):
    """Elimina un libro (richiede admin, non eliminabile se in prestito)."""
    book = db.query(models.Book).get(book_id)
    if not book:
        raise HTTPException(404, "Libro non trovato")
    active = db.query(models.Loan).filter_by(book_id=book_id, returned=False).first()
    if active:
        raise HTTPException(400, "Impossibile eliminare: libro attualmente in prestito")
    db.delete(book)
    db.commit()
    return {"ok": True}


@app.post("/books/bulk", tags=["Libri"], dependencies=[Depends(auth.require_admin)])
def bulk_import(data: BulkImport, db: Session = Depends(get_db)):
    """Importa più libri in batch (richiede admin). Salta duplicati titolo+posizione."""
    imported = 0
    skipped = 0
    for book_data in data.books:
        exists = db.query(models.Book).filter_by(
            title=book_data.title, location=book_data.location
        ).first()
        if exists:
            skipped += 1
            continue
        db.add(models.Book(**book_data.model_dump()))
        imported += 1
    db.commit()
    return {"imported": imported, "skipped": skipped}


GOOGLE_BOOKS_API_KEY = os.environ.get("GOOGLE_BOOKS_API_KEY", "")


def _clean_title(title: str) -> str:
    """Rimuove sottotitoli dopo ':' o '—', apostrofi e caratteri speciali per query più robuste."""
    short = re.split(r'[:\u2014]', title)[0].strip()
    cleaned = re.sub(r"[''`]", ' ', short)
    return cleaned.strip()


def _search_google_books(title: str, author: str) -> Optional[str]:
    """Cerca copertina su Google Books API con strategia a cascata robusta."""
    if not GOOGLE_BOOKS_API_KEY:
        return None

    author_short = author.split()[0] if author else ""
    title_clean  = _clean_title(title)

    queries = [
        f'intitle:"{title_clean}" inauthor:"{author_short}"' if author_short else None,
        f'intitle:"{title_clean}"',
        f'{title_clean} {author_short}'.strip(),
        title_clean,
        f'{title} {author_short}'.strip(),
    ]
    queries = [q for q in queries if q]

    for q in queries:
        try:
            r = httpx.get(
                "https://www.googleapis.com/books/v1/volumes",
                params={
                    "q":          q,
                    "maxResults": 5,
                    "key":        GOOGLE_BOOKS_API_KEY,
                    "fields":     "items(volumeInfo/imageLinks)",
                    "langRestrict": "it",
                },
                timeout=8,
            )
            r.raise_for_status()
            items = r.json().get("items", [])
            for item in items:
                links = item.get("volumeInfo", {}).get("imageLinks", {})
                for size in ("large", "medium", "thumbnail", "smallThumbnail"):
                    url = links.get(size)
                    if url:
                        url = url.replace("http://", "https://")
                        url = url.split("&edge=")[0]
                        return url
        except Exception:
            pass

        try:
            r = httpx.get(
                "https://www.googleapis.com/books/v1/volumes",
                params={
                    "q":          q,
                    "maxResults": 5,
                    "key":        GOOGLE_BOOKS_API_KEY,
                    "fields":     "items(volumeInfo/imageLinks)",
                },
                timeout=8,
            )
            r.raise_for_status()
            items = r.json().get("items", [])
            for item in items:
                links = item.get("volumeInfo", {}).get("imageLinks", {})
                for size in ("large", "medium", "thumbnail", "smallThumbnail"):
                    url = links.get(size)
                    if url:
                        url = url.replace("http://", "https://")
                        url = url.split("&edge=")[0]
                        return url
        except Exception:
            pass

    return None


def _search_openlibrary(title: str, author: str) -> Optional[str]:
    """Cerca copertina su Open Library. Restituisce URL o None."""
    HEADERS = {"User-Agent": "BibliotecaScolastica/3.0 (educational project)"}
    COVERS  = "https://covers.openlibrary.org/b/id/{}-M.jpg"
    author_short = author.split()[0] if author else ""

    def search(params: dict) -> Optional[str]:
        try:
            r = httpx.get(
                "https://openlibrary.org/search.json",
                params={**params, "limit": 5, "fields": "cover_i,title"},
                headers=HEADERS,
                timeout=8,
            )
            r.raise_for_status()
            for doc in r.json().get("docs", []):
                if doc.get("cover_i"):
                    return COVERS.format(doc["cover_i"])
        except Exception:
            pass
        return None

    for params in [
        {"q": f"{title} {author_short}"},
        {"title": title, "author": author_short},
        {"q": title},
        {"title": title},
    ]:
        url = search(params)
        if url:
            return url
    return None


def _search_cover_url(title: str, author: str) -> Optional[str]:
    """Google Books (primario) → Open Library (fallback)."""
    return _search_google_books(title, author) or _search_openlibrary(title, author)


# ── Job store in memoria (reset al riavvio del server) ────────────────────────
_cover_jobs: dict = {}


def _fetch_covers_job(job_id: str, book_ids: list, db_session):
    """Eseguito in un thread separato — aggiorna _cover_jobs in tempo reale."""
    job = _cover_jobs[job_id]
    try:
        for i, book_id in enumerate(book_ids):
            try:
                book = db_session.query(models.Book).get(book_id)
                if not book:
                    job["notFound"] += 1
                    job["processed"] = i + 1
                    continue
                url = _search_cover_url(book.title, book.author)
                if url:
                    book.cover_url = url
                    job["found"] += 1
                else:
                    job["notFound"] += 1
                book.openlibrary_checked = True
                db_session.commit()
            except Exception as book_err:
                db_session.rollback()
                job["notFound"] += 1
                job.setdefault("errors", []).append(
                    f"Libro {book_id}: {str(book_err)[:120]}"
                )
            finally:
                job["processed"] = i + 1

        job["remaining"] = db_session.query(models.Book).filter(
            models.Book.openlibrary_checked == False
        ).count()
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
    finally:
        db_session.close()


@app.post("/books/fetch-covers", tags=["Libri"], dependencies=[Depends(auth.require_admin)])
def fetch_covers_start(batch_size: int = 10, db: Session = Depends(get_db)):
    """
    Avvia la ricerca copertine in background.
    Restituisce subito un job_id — usa GET /books/fetch-covers/status/{job_id} per il progresso.
    """
    unchecked = (
        db.query(models.Book.id)
        .filter(models.Book.openlibrary_checked == False)
        .limit(batch_size)
        .all()
    )
    book_ids = [r[0] for r in unchecked]
    total = len(book_ids)

    if total == 0:
        return {"jobId": None, "total": 0, "remaining": 0, "message": "Nessun libro da processare"}

    job_id = str(uuid.uuid4())
    _cover_jobs[job_id] = {
        "status":    "running",
        "total":     total,
        "processed": 0,
        "found":     0,
        "notFound":  0,
        "remaining": None,
        "error":     None,
    }

    from database import SessionLocal
    thread_db = SessionLocal()
    t = threading.Thread(target=_fetch_covers_job, args=(job_id, book_ids, thread_db), daemon=True)
    t.start()

    return {"jobId": job_id, "total": total}


@app.get("/books/fetch-covers/status/{job_id}", tags=["Libri"], dependencies=[Depends(auth.require_admin)])
def fetch_covers_status(job_id: str):
    """Restituisce lo stato corrente del job di ricerca copertine."""
    job = _cover_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job non trovato")
    result = dict(job)
    errors = result.pop("errors", [])
    result["errorCount"] = len(errors)
    result["errorSample"] = errors[:5]
    return result


@app.post("/books/{book_id}/fetch-cover", tags=["Libri"], dependencies=[Depends(auth.require_admin)])
def fetch_single_cover(book_id: str, db: Session = Depends(get_db)):
    """Cerca e salva la copertina per un singolo libro appena aggiunto."""
    book = db.query(models.Book).get(book_id)
    if not book:
        raise HTTPException(404, "Libro non trovato")
    url = _search_cover_url(book.title, book.author)
    book.cover_url = url
    book.openlibrary_checked = True
    db.commit()
    db.refresh(book)
    return {"coverUrl": book.cover_url, "found": url is not None}


@app.post("/books/reset-covers-check", tags=["Libri"], dependencies=[Depends(auth.require_admin)])
def reset_covers_check(only_not_found: bool = True, db: Session = Depends(get_db)):
    """
    Resetta il flag openlibrary_checked per ritentare la ricerca.
    only_not_found=true (default): resetta solo i libri senza copertina.
    only_not_found=false: resetta tutti i libri.
    """
    query = db.query(models.Book).filter(models.Book.openlibrary_checked == True)
    if only_not_found:
        query = query.filter(
            (models.Book.cover_url == None) | (models.Book.cover_url == "")
        )
    count = query.count()
    query.update({"openlibrary_checked": False}, synchronize_session=False)
    db.commit()
    return {"reset": count}


# ══════════════════════════════════════════════════════════════════════════════
# LOANS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/loans", tags=["Prestiti"], dependencies=[Depends(auth.require_admin)])
def list_loans(returned: Optional[bool] = None, db: Session = Depends(get_db)):
    """Lista prestiti (richiede admin). returned=false → attivi, returned=true → storico."""
    query = db.query(models.Loan)
    if returned is not None:
        query = query.filter_by(returned=returned)
    loans = query.order_by(models.Loan.due_date).all()
    return [l.to_dict() for l in loans]


@app.get("/loans/taken-book-ids", tags=["Prestiti"])
def taken_book_ids(db: Session = Depends(get_db)):
    """
    Endpoint pubblico (nessuna autenticazione): restituisce SOLO la lista degli id
    dei libri attualmente in prestito, senza alcun dato personale (niente nomi,
    niente date). Serve al frontend per mostrare quali libri sono disponibili
    nella schermata di ricerca, senza dover esporre l'elenco completo dei
    prestiti (che invece resta riservato all'amministratore tramite /loans).
    """
    rows = db.query(models.Loan.book_id).filter_by(returned=False).all()
    return {"bookIds": [r[0] for r in rows]}


@app.get("/loans/user/{user}", tags=["Prestiti"])
def user_loans(user: str, db: Session = Depends(get_db),
               student_payload: dict = Depends(auth.require_student)):
    """
    Prestiti attivi dell'utente corrente. Richiede una sessione studente valida
    e restituisce SOLO i prestiti dello studente autenticato (non quelli passati
    nell'URL): {user} deve coincidere col nome verificato nel token, altrimenti
    nessun alunno potrebbe vedere i prestiti di un altro semplicemente cambiando
    il nome nell'URL.
    """
    if student_payload["name"] != user:
        raise HTTPException(403, "Non puoi vedere i prestiti di un altro alunno")
    loans = db.query(models.Loan).filter_by(user=user, returned=False).all()
    return [l.to_dict() for l in loans]


def create_notification(db: Session, recipient_type: str, recipient_id: str, kind: str,
                          title: str, body: str = "", related_loan_id: str = None) -> models.Notification:
    """
    Crea una notifica interna per uno studente (recipient_id = full_name) o
    un professore (recipient_id = Teacher.id). Sostituisce l'invio email:
    la notifica resta nel portale finché l'utente non la legge.
    """
    notif = models.Notification(
        recipient_type=recipient_type,
        recipient_id=recipient_id,
        kind=kind,
        title=title,
        body=body,
        related_loan_id=related_loan_id,
    )
    db.add(notif)
    db.commit()
    return notif


@app.post("/loans", tags=["Prestiti"])
def take_book(body: LoanCreate, db: Session = Depends(get_db),
              student_payload: dict = Depends(auth.require_student)):
    """
    Registra un nuovo prestito. Richiede una sessione studente valida (ottenuta da
    /auth/student-login): il nome dello studente viene preso dal token verificato
    dal server, NON da un campo libero passato dal client — così nessuno può
    registrare un prestito a nome di un altro alunno senza conoscerne nome+PIN.
    """
    user = student_payload["name"]

    max_loans = int(get_setting(db, SETTING_MAX_LOANS, DEFAULT_MAX_LOANS))
    active_count = db.query(models.Loan).filter_by(user=user, returned=False).count()
    if active_count >= max_loans:
        msg = "Hai già un libro in prestito. Restituiscilo prima." if max_loans == 1 \
              else f"Hai già {active_count} libri in prestito (limite: {max_loans}). Restituiscine uno prima."
        raise HTTPException(400, msg)

    book = db.query(models.Book).get(body.book_id)
    if not book:
        raise HTTPException(404, "Libro non trovato")
    taken = db.query(models.Loan).filter_by(book_id=body.book_id, returned=False).first()
    if taken:
        raise HTTPException(400, "Libro non disponibile")

    days = max(1, min(body.days, 90))
    due  = datetime.now(timezone.utc) + timedelta(days=days)

    loan = models.Loan(
        user=user,
        book_id=book.id,
        book_title=book.title,
        due_date=due,
    )
    db.add(loan)
    db.commit()
    db.refresh(loan)

    # Notifica interna di conferma prestito + email (se lo studente ha un'email)
    student = db.query(models.Student).filter_by(full_name=user).first()
    if student:
        student.last_activity_at = datetime.now(timezone.utc)
        db.commit()
        create_notification(
            db, "student", student.full_name, "loan_confirm",
            f"📖 Hai preso in prestito: {book.title}",
            f"Da restituire entro il {due.strftime('%d/%m/%Y')}.",
            related_loan_id=loan.id,
        )
        if student.email:
            email_service.send_loan_confirm(
                student_name=student.full_name,
                student_email=student.email,
                book_title=book.title,
                due_date_str=due.strftime("%d/%m/%Y"),
            )

    return loan.to_dict()


def _apply_gamification_on_return(db: Session, loan: "models.Loan") -> Optional[dict]:
    """
    Applica i punti del sistema "Topo da Biblioteca" allo studente titolare
    del prestito appena restituito. Restituisce un piccolo riepilogo
    {delta, reason, newScore} per eventuale feedback nel frontend, oppure
    None se lo studente non esiste più in anagrafica (es. prestito storico).
    """
    import gamification
    student = db.query(models.Student).filter_by(full_name=loan.user).first()
    if not student:
        return None
    new_score, score_year, delta, reason = gamification.apply_return_points(
        student.score or 0, student.score_year, loan.due_date, loan.return_date,
    )
    student.score = new_score
    student.score_year = score_year
    student.last_activity_at = loan.return_date
    db.commit()
    return {"delta": delta, "reason": reason, "newScore": new_score}


@app.patch("/loans/{loan_id}/return", tags=["Prestiti"])
def return_book(loan_id: str, db: Session = Depends(get_db),
                student_payload: dict = Depends(auth.require_student)):
    """
    Segna un prestito come restituito. Richiede una sessione studente valida:
    solo il titolare del prestito (verificato dal token) può segnarlo come
    restituito — evita che chiunque possa "liberare" il prestito di un altro
    alunno semplicemente conoscendo l'id del prestito.
    """
    loan = db.query(models.Loan).get(loan_id)
    if not loan:
        raise HTTPException(404, "Prestito non trovato")
    if loan.returned:
        raise HTTPException(400, "Già restituito")
    if loan.user != student_payload["name"]:
        raise HTTPException(403, "Questo prestito non risulta a tuo nome")

    loan.returned    = True
    loan.return_date = datetime.now(timezone.utc)
    db.commit()
    db.refresh(loan)

    gamification_result = _apply_gamification_on_return(db, loan)

    queue = db.query(models.Waitlist).filter_by(book_id=loan.book_id).order_by(models.Waitlist.date).all()
    first_in_queue = queue[0].user if queue else None
    if first_in_queue:
        _notify_waitlist_available(db, loan.book_id, loan.book_title, first_in_queue)
    return {"loan": loan.to_dict(), "nextInQueue": first_in_queue, "gamification": gamification_result}


def _notify_waitlist_available(db: Session, book_id: str, book_title: str, student_name: str):
    """Invia notifica interna + email (se disponibile) al primo studente in lista d'attesa."""
    # Notifica interna
    notif = models.Notification(
        recipient_type="student",
        recipient_id=student_name,
        kind="waitlist_available",
        title=f"📗 Disponibile: {book_title}",
        body="Il libro che aspettavi è tornato in biblioteca! Affrettati prima che lo prenda qualcun altro.",
    )
    db.add(notif)
    db.commit()
    # Email
    student = db.query(models.Student).filter_by(full_name=student_name).first()
    if student and student.email:
        html = email_service._base_template(
            "Libro disponibile!", "📗", "#2E6B45",
            f"""<p style="font-size:16px;color:#1A1208;margin:0 0 14px;">
                  Ciao <strong>{student_name}</strong>! 👋
                </p>
                <p style="font-size:15px;color:#1A1208;margin:0 0 18px;">
                  Il libro che stavi aspettando è tornato disponibile:
                </p>
                {email_service._info_table(("📖 Libro", book_title))}
                <p style="font-size:14px;color:#8B6550;margin:0;">
                  Accedi alla biblioteca e prendilo subito prima che finisca! 🏃
                </p>"""
        )
        email_service.send_email(
            to_email=student.email,
            to_name=student_name,
            subject=f"📗 Disponibile: {book_title}",
            html_body=html,
        )


@app.patch("/loans/{loan_id}/return-admin", tags=["Prestiti"], dependencies=[Depends(auth.require_admin)])
def return_book_admin(loan_id: str, db: Session = Depends(get_db)):
    """
    Segna un prestito come restituito (lato banco/admin) — utile quando l'alunno
    riporta fisicamente il libro in biblioteca e chi è al banco registra il
    rientro senza dover far rifare login allo studente.
    """
    loan = db.query(models.Loan).get(loan_id)
    if not loan:
        raise HTTPException(404, "Prestito non trovato")
    if loan.returned:
        raise HTTPException(400, "Già restituito")
    loan.returned    = True
    loan.return_date = datetime.now(timezone.utc)
    db.commit()
    db.refresh(loan)

    _apply_gamification_on_return(db, loan)

    queue = db.query(models.Waitlist).filter_by(book_id=loan.book_id).order_by(models.Waitlist.date).all()
    first_in_queue = queue[0].user if queue else None
    if first_in_queue:
        _notify_waitlist_available(db, loan.book_id, loan.book_title, first_in_queue)
    return {"loan": loan.to_dict(), "nextInQueue": first_in_queue}


@app.patch("/loans/{loan_id}/extend", tags=["Prestiti"], dependencies=[Depends(auth.require_admin)])
def extend_loan(loan_id: str, body: LoanExtend, db: Session = Depends(get_db)):
    """Estende la scadenza di un prestito (richiede admin)."""
    loan = db.query(models.Loan).get(loan_id)
    if not loan or loan.returned:
        raise HTTPException(404, "Prestito non trovato o già restituito")
    days = max(1, min(body.days, 60))
    loan.due_date = loan.due_date + timedelta(days=days)
    db.commit()
    db.refresh(loan)
    return loan.to_dict()


# ══════════════════════════════════════════════════════════════════════════════
# WAITLIST
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/waitlist", tags=["Lista d'attesa"])
def list_waitlist(db: Session = Depends(get_db)):
    entries = db.query(models.Waitlist).order_by(models.Waitlist.date).all()
    return [w.to_dict() for w in entries]


@app.post("/waitlist/{book_id}", tags=["Lista d'attesa"])
def toggle_waitlist(book_id: str, body: WaitlistToggle, db: Session = Depends(get_db),
                     student_payload: dict = Depends(auth.require_student)):
    """
    Aggiunge o rimuove l'utente dalla lista d'attesa per un libro. Richiede sessione
    studente valida; usa sempre il nome verificato dal token, ignorando body.user
    se non corrisponde (evita di iscrivere altri alla lista d'attesa a tua insaputa).
    """
    user = student_payload["name"]
    existing = db.query(models.Waitlist).filter_by(book_id=book_id, user=user).first()
    if existing:
        db.delete(existing)
        db.commit()
        return {"action": "removed"}
    entry = models.Waitlist(book_id=book_id, user=user)
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return {"action": "added", "entry": entry.to_dict()}


# ══════════════════════════════════════════════════════════════════════════════
# WISHLIST ("da leggere") — separata dalla lista d'attesa
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/wishlist/me", tags=["Wishlist"])
def my_wishlist(db: Session = Depends(get_db),
                  student_payload: dict = Depends(auth.require_student)):
    """Libri salvati dallo studente loggato come 'da leggere'."""
    items = (
        db.query(models.Wishlist)
        .filter_by(student_name=student_payload["name"])
        .order_by(models.Wishlist.added_at.desc())
        .all()
    )
    return [w.to_dict() for w in items]


@app.post("/wishlist/{book_id}", tags=["Wishlist"])
def toggle_wishlist(book_id: str, db: Session = Depends(get_db),
                      student_payload: dict = Depends(auth.require_student)):
    """
    Aggiunge o rimuove un libro dalla wishlist dello studente loggato.
    Indipendente dalla disponibilità del libro: si può salvare anche un
    libro al momento in prestito ad altri, senza generare nessuna
    prenotazione (per quella serve la lista d'attesa separata).
    """
    user = student_payload["name"]
    book = db.query(models.Book).get(book_id)
    if not book:
        raise HTTPException(404, "Libro non trovato")
    existing = db.query(models.Wishlist).filter_by(student_name=user, book_id=book_id).first()
    if existing:
        db.delete(existing)
        db.commit()
        return {"action": "removed"}
    entry = models.Wishlist(student_name=user, book_id=book_id)
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return {"action": "added", "entry": entry.to_dict()}


# ══════════════════════════════════════════════════════════════════════════════
# BACKUP / RESTORE / RESET
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/backup", tags=["Backup"], dependencies=[Depends(auth.require_admin)])
def export_backup(db: Session = Depends(get_db)):
    """Esporta tutti i dati come JSON, incluse classi, professori e alunni."""
    books    = [b.to_dict() for b in db.query(models.Book).all()]
    loans    = [l.to_dict() for l in db.query(models.Loan).all()]
    waitlist = [w.to_dict() for w in db.query(models.Waitlist).all()]
    classes  = [c.to_dict() for c in db.query(models.SchoolClass).all()]
    teachers = [t.to_dict() for t in db.query(models.Teacher).all()]
    students = [s.to_dict() for s in db.query(models.Student).all()]
    return {
        "version":    4,
        "exportDate": datetime.now(timezone.utc).isoformat(),
        "books":      books,
        "loans":      loans,
        "waitlist":   waitlist,
        "classes":    classes,
        "teachers":   teachers,
        "students":   students,
    }


@app.post("/backup/restore", tags=["Backup"], dependencies=[Depends(auth.require_admin)])
def import_backup(data: BackupData, db: Session = Depends(get_db)):
    """
    Ripristina da backup JSON. SOVRASCRIVE libri/prestiti/lista d'attesa esistenti.
    Nota: per semplicità e sicurezza, questo endpoint NON tocca classi/professori/
    alunni — quei dati si gestiscono con i loro endpoint dedicati, per evitare di
    perdere per errore l'anagrafica con PIN ed email durante un ripristino del
    solo catalogo libri.
    """
    db.query(models.Waitlist).delete()
    db.query(models.Loan).delete()
    db.query(models.Book).delete()
    db.commit()

    for b in data.books:
        db.add(models.Book(
            id=b.get("id", models.new_id()),
            title=b.get("title", ""),
            author=b.get("author", ""),
            publisher=b.get("publisher", ""),
            location=b.get("location", ""),
            genre=b.get("genre", ""),
        ))
    db.commit()

    for l in data.loans:
        def parse_dt(s):
            if not s:
                return None
            try:
                return datetime.fromisoformat(s)
            except Exception:
                return None
        db.add(models.Loan(
            id=l.get("id", models.new_id()),
            user=l.get("user", ""),
            book_id=l.get("bookId", ""),
            book_title=l.get("bookTitle", ""),
            taken_date=parse_dt(l.get("takenDate")),
            due_date=parse_dt(l.get("dueDate")) or datetime.now(timezone.utc),
            returned=l.get("returned", False),
            return_date=parse_dt(l.get("returnDate")),
        ))
    db.commit()

    for w in data.waitlist:
        db.add(models.Waitlist(
            id=w.get("id", models.new_id()),
            book_id=w.get("bookId", ""),
            user=w.get("user", ""),
        ))
    db.commit()

    return {"ok": True, "books": len(data.books), "loans": len(data.loans)}


@app.delete("/reset", tags=["Backup"], dependencies=[Depends(auth.require_admin)])
def reset_all(db: Session = Depends(get_db)):
    """Elimina libri, prestiti e lista d'attesa (irreversibile). NON tocca alunni/classi/prof."""
    db.query(models.Waitlist).delete()
    db.query(models.Loan).delete()
    db.query(models.Book).delete()
    db.commit()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# SEED (catalogo iniziale)
# ══════════════════════════════════════════════════════════════════════════════

CATALOG_SEED = [
    {"title": "Alice nel paese delle meraviglie", "author": "Carroll", "publisher": "", "location": "6.L", "genre": "fantastico"},
    {"title": "Anna Karenina", "author": "Tolstoj", "publisher": "", "location": "14.I", "genre": "narrativa / classico"},
    {"title": "Bel-ami", "author": "Maupassant", "publisher": "", "location": "7.L", "genre": "narrativa / classico"},
    {"title": "Candido e altri racconti", "author": "Voltaire", "publisher": "", "location": "10.H", "genre": "narrativa / classico"},
    {"title": "David Copperfield", "author": "Dickens", "publisher": "", "location": "24.G", "genre": "romanzo di formazione"},
    {"title": "Decameron", "author": "Boccaccio", "publisher": "", "location": "22.I", "genre": "novelle / narrativa breve"},
    {"title": "Delitto e castigo", "author": "Dostoevskij", "publisher": "", "location": "16.H", "genre": "narrativa / classico"},
    {"title": "Don Chisciotte", "author": "Cervantes", "publisher": "", "location": "26.I", "genre": "narrativa / classico"},
    {"title": "Eugenia Grandet", "author": "Balzac", "publisher": "", "location": "20.G", "genre": "narrativa / classico"},
    {"title": "Fiabe italiane", "author": "Calvino", "publisher": "", "location": "21.L", "genre": "fiabe / tradizionale"},
    {"title": "I fratelli Karamazov", "author": "Dostoevskij", "publisher": "", "location": "14.H", "genre": "narrativa / classico"},
    {"title": "I malavoglia", "author": "Verga", "publisher": "", "location": "4.I", "genre": "verismo / classico"},
    {"title": "I miserabili", "author": "Hugo", "publisher": "", "location": "14.G", "genre": "narrativa / classico"},
    {"title": "I promessi sposi", "author": "Manzoni", "publisher": "", "location": "2.H", "genre": "storico / classico"},
    {"title": "I tre moschettieri", "author": "Dumas", "publisher": "", "location": "22.G", "genre": "avventura / storico"},
    {"title": "Il barone rampante", "author": "Calvino", "publisher": "Einaudi", "location": "5.24", "genre": "narrativa / fantastico"},
    {"title": "Il deserto dei tartari", "author": "Buzzati D", "publisher": "Mondadori", "location": "43.19", "genre": "narrativa / classico"},
    {"title": "Il dottor Jekyll e mister Hyde", "author": "Stevenson", "publisher": "", "location": "10.G", "genre": "gotico / horror"},
    {"title": "Il giro del mondo in 80 giorni", "author": "Verne J", "publisher": "Fabbri", "location": "26.8", "genre": "avventura"},
    {"title": "Il piccolo principe", "author": "Saint-Exupéry", "publisher": "Bompiani", "location": "31.13", "genre": "fantastico / filosofico"},
    {"title": "Il ritratto di Dorian Gray", "author": "Wilde", "publisher": "", "location": "26.H", "genre": "gotico / narrativa"},
    {"title": "Il rosso e il nero", "author": "Stendhal", "publisher": "", "location": "5.H", "genre": "narrativa / classico"},
    {"title": "L'isola del tesoro", "author": "Stevenson R", "publisher": "Sei", "location": "14.13", "genre": "avventura"},
    {"title": "La fattoria degli animali", "author": "Orwell G", "publisher": "Mondadori", "location": "7.5", "genre": "romanzo allegorico / satira politica"},
    {"title": "La metamorfosi", "author": "Kafka F", "publisher": "Acquarelli", "location": "28.24", "genre": "narrativa / classico"},
    {"title": "Le avventure di Huckleberry Finn", "author": "Twain M", "publisher": "Mondadori", "location": "27.13", "genre": "avventura / formazione"},
    {"title": "Le avventure di Pinocchio", "author": "Collodi C", "publisher": "Cedam", "location": "25.13", "genre": "fantastico / ragazzi"},
    {"title": "Lessico famigliare", "author": "Ginzburg N", "publisher": "Einaudi", "location": "19.24", "genre": "autobiografico / classico"},
    {"title": "Madame Bovary", "author": "Flaubert", "publisher": "", "location": "12.L", "genre": "narrativa / classico"},
    {"title": "Marcovaldo", "author": "Calvino I", "publisher": "Einaudi", "location": "15.24", "genre": "narrativa / umoristico"},
    {"title": "Moby Dick", "author": "Melville", "publisher": "", "location": "8.H", "genre": "avventura / classico"},
    {"title": "Piccole donne", "author": "Alcott L M", "publisher": "Mondadori", "location": "10.13", "genre": "narrativa / formazione"},
    {"title": "Robinson Crusoe", "author": "Defoe D", "publisher": "De Agostini", "location": "1.G", "genre": "romanzo d'avventura"},
    {"title": "Se questo è un uomo", "author": "Levi P", "publisher": "Paoline", "location": "27.19", "genre": "storico / testimonianza"},
    {"title": "Cuore", "author": "De Amicis E", "publisher": "Sei", "location": "13.13", "genre": "narrativa / formazione"},
    {"title": "Fahrenheit 451", "author": "Bradbury R", "publisher": "Mondadori", "location": "25.24", "genre": "fantascienza"},
    {"title": "Dracula", "author": "Stoker B", "publisher": "Lattes", "location": "27.22", "genre": "horror / classico"},
    {"title": "Dieci piccoli indiani", "author": "Christie A", "publisher": "Paoline", "location": "21.20", "genre": "giallo / classico"},
    {"title": "Momo", "author": "Ende M", "publisher": "Longanesi", "location": "15.18", "genre": "romanzo fantastico / filosofico"},
    {"title": "Storia di una gabbianella e del gatto", "author": "Sepulveda L", "publisher": "Salani", "location": "48.16", "genre": "favola / narrativa"},
    {"title": "La solitudine dei numeri primi", "author": "Giordano P", "publisher": "Mondadori", "location": "10.22", "genre": "narrativa / drammatico"},
    {"title": "Novelle per un anno", "author": "Pirandello L", "publisher": "Bibl. popolare", "location": "1.23", "genre": "racconti / classico"},
    {"title": "Cuore d'inchiostro", "author": "Funke C", "publisher": "Mondadori", "location": "45.20", "genre": "fantasy"},
]


@app.post("/seed", tags=["Setup"], dependencies=[Depends(auth.require_admin)])
def seed_catalog(db: Session = Depends(get_db)):
    """Popola il catalogo iniziale se vuoto (richiede admin)."""
    if db.query(models.Book).count() > 0:
        return {"message": "Catalogo già popolato", "count": db.query(models.Book).count()}
    for b in CATALOG_SEED:
        db.add(models.Book(**b))
    db.commit()
    return {"message": "Catalogo popolato", "count": len(CATALOG_SEED)}


@app.get("/", tags=["Info"])
def root():
    return {"name": "Biblioteca Scolastica API", "version": "3.0.0", "docs": "/docs"}


@app.get("/health", tags=["Info"])
def health():
    """Endpoint leggero per i controlli di uptime/keep-alive del provider di hosting."""
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


# ══════════════════════════════════════════════════════════════════════════════
# SETTINGS (chiave-valore runtime)
# ══════════════════════════════════════════════════════════════════════════════

SETTING_PIN_AUTH  = "pin_auth_enabled"
SETTING_MAX_LOANS = "max_loans_per_user"
DEFAULT_MAX_LOANS = "1"


def get_setting(db: Session, key: str, default: str = "false") -> str:
    row = db.query(models.Setting).get(key)
    return row.value if row else default


def set_setting(db: Session, key: str, value: str):
    row = db.query(models.Setting).get(key)
    if row:
        row.value = value
    else:
        db.add(models.Setting(key=key, value=value))
    db.commit()


@app.get("/settings", tags=["Settings"])
def read_settings(db: Session = Depends(get_db)):
    """Legge le impostazioni pubbliche (senza auth — il frontend le serve al caricamento)."""
    return {
        "pinAuthEnabled":  get_setting(db, SETTING_PIN_AUTH) == "true",
        "maxLoansPerUser": int(get_setting(db, SETTING_MAX_LOANS, DEFAULT_MAX_LOANS)),
    }


@app.patch("/settings", tags=["Settings"], dependencies=[Depends(auth.require_admin)])
def update_settings(body: dict, db: Session = Depends(get_db)):
    """Aggiorna impostazioni (richiede admin)."""
    if "pinAuthEnabled" in body:
        set_setting(db, SETTING_PIN_AUTH, "true" if body["pinAuthEnabled"] else "false")
    if "maxLoansPerUser" in body:
        val = max(1, min(int(body["maxLoansPerUser"]), 10))
        set_setting(db, SETTING_MAX_LOANS, str(val))
    return {
        "pinAuthEnabled":  get_setting(db, SETTING_PIN_AUTH) == "true",
        "maxLoansPerUser": int(get_setting(db, SETTING_MAX_LOANS, DEFAULT_MAX_LOANS)),
    }


# ══════════════════════════════════════════════════════════════════════════════
# CLASSI
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/classes", tags=["Classi"], dependencies=[Depends(auth.require_admin)])
def list_classes(db: Session = Depends(get_db)):
    """Lista tutte le classi (richiede admin)."""
    classes = db.query(models.SchoolClass).order_by(models.SchoolClass.name).all()
    return [c.to_dict() for c in classes]


@app.post("/classes", tags=["Classi"], dependencies=[Depends(auth.require_admin)])
def create_class(body: ClassCreate, db: Session = Depends(get_db)):
    """Crea una nuova classe (es. '3B')."""
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Nome classe obbligatorio")
    exists = db.query(models.SchoolClass).filter_by(name=name).first()
    if exists:
        raise HTTPException(400, f'Classe "{name}" già presente')
    school_class = models.SchoolClass(name=name, school_year=body.school_year.strip())
    db.add(school_class)
    db.commit()
    db.refresh(school_class)
    return school_class.to_dict()


@app.patch("/classes/{class_id}", tags=["Classi"], dependencies=[Depends(auth.require_admin)])
def update_class(class_id: str, body: ClassUpdate, db: Session = Depends(get_db)):
    """Rinomina una classe o aggiorna l'anno scolastico."""
    school_class = db.query(models.SchoolClass).get(class_id)
    if not school_class:
        raise HTTPException(404, "Classe non trovata")
    if body.name is not None:
        new_name = body.name.strip()
        if not new_name:
            raise HTTPException(400, "Nome classe non può essere vuoto")
        dup = db.query(models.SchoolClass).filter(
            models.SchoolClass.name == new_name, models.SchoolClass.id != class_id
        ).first()
        if dup:
            raise HTTPException(400, f'Classe "{new_name}" già presente')
        school_class.name = new_name
    if body.school_year is not None:
        school_class.school_year = body.school_year.strip()
    db.commit()
    db.refresh(school_class)
    return school_class.to_dict()


@app.delete("/classes/{class_id}", tags=["Classi"], dependencies=[Depends(auth.require_admin)])
def delete_class(class_id: str, db: Session = Depends(get_db)):
    """
    Elimina una classe. Bloccato se ci sono ancora alunni assegnati: bisogna
    prima spostarli in un'altra classe o eliminarli, per non lasciare alunni
    "orfani" senza classe (obbligatoria per regola del sistema).
    """
    school_class = db.query(models.SchoolClass).get(class_id)
    if not school_class:
        raise HTTPException(404, "Classe non trovata")
    student_count = db.query(models.Student).filter_by(class_id=class_id).count()
    if student_count > 0:
        raise HTTPException(
            400,
            f'Impossibile eliminare: ci sono ancora {student_count} alunni in questa classe. '
            'Spostali in un\'altra classe prima di eliminarla.',
        )
    # Rimuove anche le associazioni con i professori
    db.query(models.TeacherClass).filter_by(class_id=class_id).delete()
    db.delete(school_class)
    db.commit()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# PROFESSORI
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/teachers", tags=["Professori"], dependencies=[Depends(auth.require_admin)])
def list_teachers(db: Session = Depends(get_db)):
    """Lista tutti i professori con le rispettive classi (richiede admin)."""
    teachers = db.query(models.Teacher).order_by(models.Teacher.last_name, models.Teacher.first_name).all()
    return [t.to_dict() for t in teachers]


def _resolve_classes(db: Session, class_ids: list[str]) -> list[models.SchoolClass]:
    if not class_ids:
        return []
    classes = db.query(models.SchoolClass).filter(models.SchoolClass.id.in_(class_ids)).all()
    found_ids = {c.id for c in classes}
    missing = [cid for cid in class_ids if cid not in found_ids]
    if missing:
        raise HTTPException(400, f"Classi non trovate: {', '.join(missing)}")
    return classes


@app.post("/teachers", tags=["Professori"], dependencies=[Depends(auth.require_admin)])
def create_teacher(body: TeacherCreate, db: Session = Depends(get_db)):
    """Crea un nuovo professore. Nome e cognome sono obbligatori e devono essere univoci."""
    first_name = body.first_name.strip()
    last_name  = body.last_name.strip()
    if not first_name or not last_name:
        raise HTTPException(400, "Nome e cognome sono obbligatori")

    full_name = f"{first_name} {last_name}"
    exists = db.query(models.Teacher).filter_by(full_name=full_name).first()
    if exists:
        raise HTTPException(
            400,
            f'Un professore "{full_name}" è già registrato. In caso di omonimia, '
            'aggiungi un\'iniziale o un numero per distinguerli (es. "Maria Rossi B").',
        )

    classes = _resolve_classes(db, body.class_ids)

    teacher = models.Teacher(first_name=first_name, last_name=last_name, full_name=full_name)
    teacher.classes = classes
    db.add(teacher)
    db.commit()
    db.refresh(teacher)
    return teacher.to_dict()


@app.patch("/teachers/{teacher_id}", tags=["Professori"], dependencies=[Depends(auth.require_admin)])
def update_teacher(teacher_id: str, body: TeacherUpdate, db: Session = Depends(get_db)):
    """Modifica un professore: dati, classi assegnate o stato attivo."""
    teacher = db.query(models.Teacher).get(teacher_id)
    if not teacher:
        raise HTTPException(404, "Professore non trovato")
    if body.first_name is not None:
        v = body.first_name.strip()
        if not v:
            raise HTTPException(400, "Nome non può essere vuoto")
        teacher.first_name = v
    if body.last_name is not None:
        v = body.last_name.strip()
        if not v:
            raise HTTPException(400, "Cognome non può essere vuoto")
        teacher.last_name = v
    if body.first_name is not None or body.last_name is not None:
        new_full_name = f"{teacher.first_name} {teacher.last_name}"
        dup = db.query(models.Teacher).filter(
            models.Teacher.full_name == new_full_name, models.Teacher.id != teacher_id
        ).first()
        if dup:
            raise HTTPException(400, f'Un professore "{new_full_name}" è già registrato.')
        teacher.full_name = new_full_name
    if body.active is not None:
        teacher.active = body.active
    if body.class_ids is not None:
        teacher.classes = _resolve_classes(db, body.class_ids)
    db.commit()
    db.refresh(teacher)
    return teacher.to_dict()


@app.delete("/teachers/{teacher_id}", tags=["Professori"], dependencies=[Depends(auth.require_admin)])
def delete_teacher(teacher_id: str, db: Session = Depends(get_db)):
    """Elimina un professore."""
    teacher = db.query(models.Teacher).get(teacher_id)
    if not teacher:
        raise HTTPException(404, "Professore non trovato")
    db.query(models.TeacherClass).filter_by(teacher_id=teacher_id).delete()
    db.delete(teacher)
    db.commit()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# STUDENTS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/students", tags=["Alunni"], dependencies=[Depends(auth.require_admin)])
def list_students(class_id: Optional[str] = None, db: Session = Depends(get_db)):
    """Lista tutti gli alunni (richiede admin). Filtro opzionale per classe."""
    query = db.query(models.Student)
    if class_id:
        query = query.filter_by(class_id=class_id)
    students = query.order_by(models.Student.full_name).all()
    active_loans = {l.user: l for l in db.query(models.Loan).filter_by(returned=False).all()}
    result = []
    for s in students:
        d = s.to_dict()
        loan = active_loans.get(s.full_name)
        d["activeLoan"] = loan.book_title if loan else None
        result.append(d)
    return result


@app.post("/students", tags=["Alunni"], dependencies=[Depends(auth.require_admin)])
def create_student(body: StudentCreate, db: Session = Depends(get_db)):
    """Crea un nuovo alunno con PIN generato automaticamente. La classe è obbligatoria."""
    full_name = body.full_name.strip()
    if not full_name:
        raise HTTPException(400, "Nome obbligatorio")
    if not body.class_id or not body.class_id.strip():
        raise HTTPException(400, "La classe è obbligatoria")

    school_class = db.query(models.SchoolClass).get(body.class_id)
    if not school_class:
        raise HTTPException(400, "Classe non trovata")

    exists = db.query(models.Student).filter_by(full_name=full_name).first()
    if exists:
        raise HTTPException(400, f'Alunno "{full_name}" già presente')

    student = models.Student(
        full_name=full_name, notes=body.notes, class_id=body.class_id,
    )
    db.add(student)
    db.commit()
    db.refresh(student)
    return student.to_dict()


@app.patch("/students/{student_id}", tags=["Alunni"], dependencies=[Depends(auth.require_admin)])
def update_student(student_id: str, body: StudentUpdate, db: Session = Depends(get_db)):
    """Modifica note, stato, classe, full_name ed email (lato admin)."""
    student = db.query(models.Student).get(student_id)
    if not student:
        raise HTTPException(404, "Alunno non trovato")
    if body.active is False:
        loan = db.query(models.Loan).filter_by(user=student.full_name, returned=False).first()
        if loan:
            raise HTTPException(400, f'Impossibile disattivare: "{student.full_name}" ha un prestito in corso ({loan.book_title})')
    if body.full_name is not None:
        new_name = body.full_name.strip()
        if not new_name:
            raise HTTPException(400, "Il nome non può essere vuoto")
        dup = db.query(models.Student).filter(
            models.Student.full_name == new_name,
            models.Student.id != student_id
        ).first()
        if dup:
            raise HTTPException(400, f'Esiste già un alunno con nome "{new_name}"')
        # Aggiorna anche i riferimenti nei prestiti
        db.query(models.Loan).filter_by(user=student.full_name).update({"user": new_name})
        db.query(models.Waitlist).filter_by(user=student.full_name).update({"user": new_name})
        student.full_name = new_name
    if body.active is not None:
        student.active = body.active
    if body.notes is not None:
        student.notes = body.notes
    if body.email is not None:
        student.email = body.email.strip().lower() or None
    if body.class_id is not None:
        if not body.class_id.strip():
            raise HTTPException(400, "La classe è obbligatoria, non può essere vuota")
        school_class = db.query(models.SchoolClass).get(body.class_id)
        if not school_class:
            raise HTTPException(400, "Classe non trovata")
        student.class_id = body.class_id
    db.commit()
    db.refresh(student)
    return student.to_dict()


@app.patch("/admin/students/{student_id}/email", tags=["Alunni"],
           dependencies=[Depends(auth.require_admin)])
def admin_update_student_email(student_id: str, body: StudentEmailUpdate,
                                db: Session = Depends(get_db)):
    """Aggiorna l'email di uno studente (lato admin)."""
    student = db.query(models.Student).get(student_id)
    if not student:
        raise HTTPException(404, "Alunno non trovato")
    student.email = body.email.strip().lower() if body.email else None
    db.commit()
    return {"ok": True, "email": student.email}


@app.post("/students/{student_id}/reset-pin", tags=["Alunni"], dependencies=[Depends(auth.require_admin)])
def reset_pin(student_id: str, db: Session = Depends(get_db)):
    """Rigenera il PIN dell'alunno (casuale)."""
    student = db.query(models.Student).get(student_id)
    if not student:
        raise HTTPException(404, "Alunno non trovato")
    student.pin = models.generate_pin()
    db.commit()
    db.refresh(student)
    return student.to_dict()


class PinSet(BaseModel):
    pin: str

    @field_validator("pin")
    @classmethod
    def validate_pin(cls, v):
        if not v.isdigit() or len(v) != 5:
            raise ValueError("Il PIN deve essere esattamente 5 cifre numeriche")
        return v


@app.patch("/students/{student_id}/set-pin", tags=["Alunni"], dependencies=[Depends(auth.require_admin)])
def set_pin_manual(student_id: str, body: PinSet, db: Session = Depends(get_db)):
    """Imposta manualmente il PIN dell'alunno (5 cifre numeriche)."""
    student = db.query(models.Student).get(student_id)
    if not student:
        raise HTTPException(404, "Alunno non trovato")
    student.pin = body.pin
    db.commit()
    db.refresh(student)
    return student.to_dict()


@app.delete("/students/{student_id}", tags=["Alunni"], dependencies=[Depends(auth.require_admin)])
def delete_student(student_id: str, db: Session = Depends(get_db)):
    """Elimina un alunno (solo se non ha prestiti attivi)."""
    student = db.query(models.Student).get(student_id)
    if not student:
        raise HTTPException(404, "Alunno non trovato")
    loan = db.query(models.Loan).filter_by(user=student.full_name, returned=False).first()
    if loan:
        raise HTTPException(400, f'Impossibile eliminare: ha un prestito in corso ({loan.book_title})')
    db.delete(student)
    db.commit()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# SCHEDULER — controllo giornaliero prestiti (promemoria e ritardi)
# ══════════════════════════════════════════════════════════════════════════════
#
# Logica notifiche interne (sostituiscono le email, 4 tipi):
#   1) Conferma prestito       → allo studente, subito al momento del prestito
#                                 (gestita in take_book, non qui)
#   2) Reminder pre-scadenza   → allo studente, 1 giorno PRIMA della scadenza,
#                                 una sola volta per prestito
#   3) Promemoria ritardo      → allo studente, OGNI giorno dopo la scadenza,
#                                 finché non restituisce
#   4) Alert professore        → ai prof della classe dell'alunno, esattamente
#                                 2 giorni e 5 giorni DOPO la scadenza (due
#                                 invii fissi, non ripetuti ogni giorno)
#
# Se un prestito viene restituito prima che scattino questi giorni, il
# controllo lo ignora semplicemente (filtra solo returned == False): nessuna
# notifica "tardiva" può mai essere creata per un prestito già chiuso.

DAYS_BEFORE_DUE_REMINDER = 1   # reminder allo studente, 1 giorno prima della scadenza
TEACHER_FIRST_ALERT_DAYS  = 2   # primo alert al prof, esattamente 2 giorni dopo la scadenza
TEACHER_SECOND_ALERT_DAYS = 5   # richiamo al prof, esattamente 5 giorni dopo la scadenza
CLASS_RECOMMENDATION_DAYS = 30  # un consiglio di lettura scade ed è eliminato dopo questi giorni


def check_due_soon_reminders():
    """
    Eseguita ogni giorno dallo scheduler. Crea la notifica per lo studente
    per i prestiti che scadono esattamente domani (DAYS_BEFORE_DUE_REMINDER
    giorni da oggi), una sola volta per prestito.
    """
    from database import SessionLocal
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        target_date = (now + timedelta(days=DAYS_BEFORE_DUE_REMINDER)).date()

        active_loans = db.query(models.Loan).filter(
            models.Loan.returned == False,
            models.Loan.last_student_reminder_at.is_(None),  # non ancora notificato
        ).all()

        sent_count = 0
        for loan in active_loans:
            if loan.due_date.date() != target_date:
                continue
            student = db.query(models.Student).filter_by(full_name=loan.user).first()
            if not student:
                continue
            due_str = loan.due_date.strftime("%d/%m/%Y")
            create_notification(
                db, "student", student.full_name, "due_soon",
                f"⏰ Domani scade: {loan.book_title}",
                f"Ricordati di riportarlo in biblioteca entro il {due_str}.",
                related_loan_id=loan.id,
            )
            loan.last_student_reminder_at = now
            db.commit()
            # Email (se lo studente ha un'email)
            if student.email:
                email_service.send_due_soon(
                    student_name=student.full_name,
                    student_email=student.email,
                    book_title=loan.book_title,
                    due_date_str=due_str,
                )
            sent_count += 1

        logger.info("Reminder pre-scadenza: %d notifiche create", sent_count)
    except Exception as exc:
        logger.error("Errore durante la creazione dei reminder pre-scadenza: %s", exc)
        db.rollback()
    finally:
        db.close()


def check_overdue_loans():
    """Eseguita ogni giorno dallo scheduler. Apre una propria sessione DB."""
    from database import SessionLocal
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        today = now.date()
        overdue_loans = db.query(models.Loan).filter(
            models.Loan.returned == False,
            models.Loan.due_date < now,
        ).all()

        logger.info("Controllo ritardi: %d prestiti scaduti trovati", len(overdue_loans))

        for loan in overdue_loans:
            days_late = (now - loan.due_date).days
            student = db.query(models.Student).filter_by(full_name=loan.user).first()
            if not student:
                continue  # prestito storico di un nome non più in anagrafica

            due_str = loan.due_date.strftime("%d/%m/%Y")
            giorno_o_giorni = "giorno" if days_late == 1 else "giorni"

            # ── Notifica allo studente: OGNI giorno di ritardo ────────────────
            # Confrontiamo la DATA (non l'orario) dell'ultima notifica creata
            # con oggi, per garantire al massimo una notifica al giorno anche
            # se lo scheduler dovesse girare più volte nella stessa giornata.
            already_notified_today = (
                loan.last_student_reminder_at is not None
                and loan.last_student_reminder_at.date() == today
            )
            if not already_notified_today:
                create_notification(
                    db, "student", student.full_name, "overdue",
                    f"📕 In ritardo: {loan.book_title}",
                    f"Scadenza {due_str} — in ritardo di {days_late} {giorno_o_giorni}. Riportalo appena possibile.",
                    related_loan_id=loan.id,
                )
                loan.last_student_reminder_at = now
                db.commit()
                # Email allo studente (se ha un'email)
                if student.email:
                    email_service.send_overdue_student(
                        student_name=student.full_name,
                        student_email=student.email,
                        book_title=loan.book_title,
                        due_date_str=due_str,
                        days_late=days_late,
                    )

            # ── Alert ai professori: a 2 e 5 giorni di ritardo ────────────────
            if student.class_id:
                stage = loan.teacher_alert_stage or 0
                new_stage = None
                if stage < 1 and days_late >= TEACHER_FIRST_ALERT_DAYS:
                    new_stage = 1
                elif stage < 2 and days_late >= TEACHER_SECOND_ALERT_DAYS:
                    new_stage = 2

                if new_stage is not None:
                    school_class = db.query(models.SchoolClass).get(student.class_id)
                    if school_class and school_class.teachers:
                        is_followup = new_stage == 2
                        title = (
                            f"🔴 Richiamo: {student.full_name} ({school_class.name}) — ancora in ritardo"
                            if is_followup else
                            f"⚠️ Ritardo: {student.full_name} ({school_class.name})"
                        )
                        body = (
                            f"Libro: {loan.book_title} — Scadenza: {due_str} — "
                            f"Ritardo: {days_late} giorni."
                        )
                        any_created = False
                        for teacher in school_class.teachers:
                            if not teacher.active:
                                continue
                            create_notification(
                                db, "teacher", teacher.id, "teacher_alert",
                                title, body, related_loan_id=loan.id,
                            )
                            # Email al professore (se ha un'email)
                            if teacher.email:
                                email_service.send_overdue_teacher(
                                    teacher_name=teacher.full_name,
                                    teacher_email=teacher.email,
                                    student_name=student.full_name,
                                    class_name=school_class.name,
                                    book_title=loan.book_title,
                                    due_date_str=due_str,
                                    days_late=days_late,
                                    is_followup=is_followup,
                                )
                            any_created = True
                        if any_created:
                            loan.last_teacher_alert_at = now
                            loan.teacher_alert_stage = new_stage
                            db.commit()

    except Exception as exc:
        logger.error("Errore durante il controllo ritardi: %s", exc)
        db.rollback()
    finally:
        db.close()


def check_inactivity_penalties():
    """
    Eseguita ogni giorno dallo scheduler. Applica la penalità "Topo da
    Biblioteca" agli alunni attivi che non hanno alcuna attività di prestito
    da almeno INACTIVITY_PERIOD_DAYS giorni (né un prestito in corso né una
    restituzione recente). Chi non ha mai preso in prestito nulla (nessuna
    last_activity_at) non viene penalizzato: la penalità si applica solo a
    chi ha già iniziato a usare la biblioteca e poi si è fermato.
    """
    import gamification
    from database import SessionLocal
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        students = db.query(models.Student).filter(
            models.Student.active == True,
            models.Student.last_activity_at.isnot(None),
        ).all()

        penalized_count = 0
        for student in students:
            new_score, score_year, new_penalty_at, applied = gamification.apply_inactivity_penalty(
                student.score or 0, student.score_year,
                student.last_activity_at, student.last_inactivity_penalty_at, now,
            )
            if applied:
                student.score = new_score
                student.score_year = score_year
                student.last_inactivity_penalty_at = new_penalty_at
                penalized_count += 1
        if penalized_count:
            db.commit()
        logger.info("Controllo inattività: %d alunni penalizzati", penalized_count)
    except Exception as exc:
        logger.error("Errore durante il controllo inattività: %s", exc)
        db.rollback()
    finally:
        db.close()


def check_expired_recommendations():
    """
    Eseguita ogni giorno dallo scheduler. Elimina definitivamente i consigli
    di lettura dei professori scaduti (oltre CLASS_RECOMMENDATION_DAYS giorni
    dall'impostazione). Nessun rinnovo automatico: se il prof lo vuole ancora
    attivo deve reimpostarlo da capo dal suo portale.
    """
    from database import SessionLocal
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        deleted = db.query(models.ClassRecommendation).filter(
            models.ClassRecommendation.expires_at.isnot(None),
            models.ClassRecommendation.expires_at < now,
        ).delete(synchronize_session=False)
        db.commit()
        if deleted:
            logger.info("Consigli di lettura scaduti eliminati: %d", deleted)
    except Exception as exc:
        logger.error("Errore durante la pulizia dei consigli di lettura scaduti: %s", exc)
        db.rollback()
    finally:
        db.close()


def _start_scheduler():
    """Avvia lo scheduler in background (un controllo al giorno)."""
    if os.environ.get("DISABLE_SCHEDULER", "").lower() == "true":
        logger.info("Scheduler disattivato via DISABLE_SCHEDULER=true")
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler(timezone="Europe/Rome")
        # Ogni giorno alle 8:00 (orario scolastico, prima delle lezioni)
        scheduler.add_job(check_due_soon_reminders, "cron", hour=7, minute=50)
        scheduler.add_job(check_overdue_loans, "cron", hour=8, minute=0)
        scheduler.add_job(check_inactivity_penalties, "cron", hour=8, minute=10)
        scheduler.add_job(check_expired_recommendations, "cron", hour=8, minute=20)
        scheduler.start()
        logger.info("Scheduler avviato: controllo ritardi ogni giorno alle 08:00")
    except Exception as exc:
        logger.error("Impossibile avviare lo scheduler: %s", exc)


_start_scheduler()


@app.post("/admin/check-due-soon-now", tags=["Setup"], dependencies=[Depends(auth.require_admin)])
def trigger_due_soon_check_now():
    """Esegue subito l'invio dei reminder pre-scadenza, senza aspettare il giro automatico."""
    check_due_soon_reminders()
    return {"ok": True, "message": "Controllo reminder pre-scadenza eseguito"}


@app.post("/admin/check-overdue-now", tags=["Setup"], dependencies=[Depends(auth.require_admin)])
def trigger_overdue_check_now():
    """
    Esegue subito il controllo ritardi/invio email, senza aspettare il prossimo
    giro automatico. Utile per testare la configurazione email o per un invio
    manuale immediato.
    """
    check_overdue_loans()
    return {"ok": True, "message": "Controllo ritardi eseguito"}


@app.post("/admin/check-inactivity-now", tags=["Setup"], dependencies=[Depends(auth.require_admin)])
def trigger_inactivity_check_now():
    """Esegue subito il controllo penalità da inattività, senza aspettare il giro automatico."""
    check_inactivity_penalties()
    return {"ok": True, "message": "Controllo inattività eseguito"}


@app.post("/admin/check-expired-recommendations-now", tags=["Setup"], dependencies=[Depends(auth.require_admin)])
def trigger_expired_recommendations_check_now():
    """Esegue subito la pulizia dei consigli di lettura scaduti, senza aspettare il giro automatico."""
    check_expired_recommendations()
    return {"ok": True, "message": "Pulizia consigli scaduti eseguita"}


# ══════════════════════════════════════════════════════════════════════════════
# GAMIFICATION — "Topo da Biblioteca"
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/students/me", tags=["Auth"])
def my_profile(db: Session = Depends(get_db),
                student_payload: dict = Depends(auth.require_student)):
    """
    Restituisce il profilo completo dello studente attualmente loggato:
    nome, cognome, classe, docenti collegati alla classe, libri in prestito,
    gamification (punteggio/fiamma), recensioni lasciate, storico ritardi.
    Nome, cognome e classe non sono modificabili dallo studente: solo
    l'admin può cambiarli.
    """
    import gamification
    student = db.query(models.Student).filter_by(full_name=student_payload["name"]).first()
    if not student:
        raise HTTPException(404, "Alunno non trovato")

    # Nome/cognome separati dal full_name ("Nome Cognome" o "Nome Secondo Cognome")
    parts = student.full_name.split(" ")
    first_name = parts[0]
    last_name = " ".join(parts[1:]) if len(parts) > 1 else ""

    # Docenti collegati alla classe dello studente
    teachers = []
    if student.school_class:
        teachers = [
            {"id": t.id, "fullName": t.full_name}
            for t in student.school_class.teachers if t.active
        ]

    # Prestiti attivi
    active_loans = (
        db.query(models.Loan)
        .filter_by(user=student.full_name, returned=False)
        .order_by(models.Loan.due_date)
        .all()
    )

    # Gamification
    new_score, score_year = gamification.reset_if_new_year(student.score or 0, student.score_year)
    level = gamification.get_level_info(new_score)

    # Recensioni lasciate
    reviews = (
        db.query(models.Review)
        .filter_by(student_name=student.full_name)
        .order_by(models.Review.created_at.desc())
        .all()
    )

    # Storico ritardi: prestiti già restituiti in ritardo, o attivi e già scaduti
    now = datetime.now(timezone.utc)
    all_loans = db.query(models.Loan).filter_by(user=student.full_name).all()
    late_history = []
    for loan in all_loans:
        if loan.returned and loan.return_date and loan.return_date > loan.due_date:
            days_late = (loan.return_date - loan.due_date).days
            late_history.append({
                "bookTitle": loan.book_title, "dueDate": loan.due_date.isoformat(),
                "returnDate": loan.return_date.isoformat(), "daysLate": days_late, "ongoing": False,
            })
        elif not loan.returned and loan.due_date < now:
            days_late = (now - loan.due_date).days
            late_history.append({
                "bookTitle": loan.book_title, "dueDate": loan.due_date.isoformat(),
                "returnDate": None, "daysLate": days_late, "ongoing": True,
            })
    late_history.sort(key=lambda r: r["dueDate"], reverse=True)

    return {
        "firstName": first_name,
        "lastName": last_name,
        "fullName": student.full_name,
        "email": student.email or "",
        "className": student.school_class.name if student.school_class else "—",
        "classId": student.class_id,
        "teachers": teachers,
        "activeLoans": [l.to_dict() for l in active_loans],
        "gamification": {
            "score": new_score, "scoreYear": score_year,
            "flames": level["flames"], "title": level["title"],
            "scoreMax": gamification.SCORE_MAX,
        },
        "reviews": [r.to_dict() for r in reviews],
        "lateHistory": late_history,
    }


@app.get("/students/me/score", tags=["Gamification"])
def my_score(db: Session = Depends(get_db),
             student_payload: dict = Depends(auth.require_student)):
    """
    Restituisce punteggio, livello (fiamme) e titolo dello studente attualmente
    loggato. Applica anche, al volo, il reset automatico se l'anno scolastico
    è cambiato da quando non si era fatto nessun login (così il recap che
    l'alunno vede al primo accesso del nuovo anno è già corretto).
    """
    import gamification
    full_name = student_payload["name"]
    student = db.query(models.Student).filter_by(full_name=full_name).first()
    if not student:
        raise HTTPException(404, "Alunno non trovato")

    new_score, score_year = gamification.reset_if_new_year(student.score or 0, student.score_year)
    if score_year != student.score_year:
        student.score = new_score
        student.score_year = score_year
        db.commit()

    level = gamification.get_level_info(student.score or 0)
    return {
        "score":     student.score or 0,
        "scoreYear": student.score_year,
        "flames":    level["flames"],
        "title":     level["title"],
        "scoreMax":  gamification.SCORE_MAX,
    }

# ══════════════════════════════════════════════════════════════════════════════
# NOTIFICHE INTERNE (portale, sostituiscono le email)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/notifications/me", tags=["Notifiche"])
def my_notifications(db: Session = Depends(get_db),
                       student_payload: dict = Depends(auth.require_student)):
    """Notifiche dello studente attualmente loggato, più recenti prima."""
    full_name = student_payload["name"]
    notifs = (
        db.query(models.Notification)
        .filter_by(recipient_type="student", recipient_id=full_name)
        .order_by(models.Notification.created_at.desc())
        .limit(50)
        .all()
    )
    unread_count = db.query(models.Notification).filter_by(
        recipient_type="student", recipient_id=full_name, read=False,
    ).count()
    return {"notifications": [n.to_dict() for n in notifs], "unreadCount": unread_count}


@app.patch("/notifications/{notification_id}/read", tags=["Notifiche"])
def mark_notification_read(notification_id: str, db: Session = Depends(get_db),
                             student_payload: dict = Depends(auth.require_student)):
    """Segna una notifica studente come letta (solo se appartiene a lui)."""
    notif = db.query(models.Notification).get(notification_id)
    if not notif or notif.recipient_type != "student" or notif.recipient_id != student_payload["name"]:
        raise HTTPException(404, "Notifica non trovata")
    notif.read = True
    db.commit()
    return {"ok": True}


@app.post("/notifications/mark-all-read", tags=["Notifiche"])
def mark_all_notifications_read(db: Session = Depends(get_db),
                                  student_payload: dict = Depends(auth.require_student)):
    """Segna tutte le notifiche dello studente come lette."""
    db.query(models.Notification).filter_by(
        recipient_type="student", recipient_id=student_payload["name"], read=False,
    ).update({"read": True})
    db.commit()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# PORTALE PROFESSORI
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/teacher/me", tags=["Portale Professori"])
def teacher_my_profile(db: Session = Depends(get_db),
                         teacher_payload: dict = Depends(auth.require_teacher)):
    """Profilo del professore attualmente loggato."""
    teacher = db.query(models.Teacher).get(teacher_payload["teacher_id"])
    if not teacher:
        raise HTTPException(404, "Professore non trovato")
    return teacher.to_dict()


@app.get("/teacher/notifications", tags=["Portale Professori"])
def teacher_notifications(db: Session = Depends(get_db),
                            teacher_payload: dict = Depends(auth.require_teacher)):
    """Notifiche del professore attualmente loggato, più recenti prima."""
    teacher_id = teacher_payload["teacher_id"]
    notifs = (
        db.query(models.Notification)
        .filter_by(recipient_type="teacher", recipient_id=teacher_id)
        .order_by(models.Notification.created_at.desc())
        .limit(100)
        .all()
    )
    unread_count = db.query(models.Notification).filter_by(
        recipient_type="teacher", recipient_id=teacher_id, read=False,
    ).count()
    return {"notifications": [n.to_dict() for n in notifs], "unreadCount": unread_count}


@app.patch("/teacher/notifications/{notification_id}/read", tags=["Portale Professori"])
def teacher_mark_notification_read(notification_id: str, db: Session = Depends(get_db),
                                     teacher_payload: dict = Depends(auth.require_teacher)):
    """Segna una notifica del professore come letta."""
    notif = db.query(models.Notification).get(notification_id)
    if not notif or notif.recipient_type != "teacher" or notif.recipient_id != teacher_payload["teacher_id"]:
        raise HTTPException(404, "Notifica non trovata")
    notif.read = True
    db.commit()
    return {"ok": True}


@app.post("/teacher/notifications/mark-all-read", tags=["Portale Professori"])
def teacher_mark_all_read(db: Session = Depends(get_db),
                            teacher_payload: dict = Depends(auth.require_teacher)):
    """Segna tutte le notifiche del professore come lette."""
    db.query(models.Notification).filter_by(
        recipient_type="teacher", recipient_id=teacher_payload["teacher_id"], read=False,
    ).update({"read": True})
    db.commit()
    return {"ok": True}


@app.get("/teacher/classes/{class_id}/students", tags=["Portale Professori"])
def teacher_class_students(class_id: str, db: Session = Depends(get_db),
                             teacher_payload: dict = Depends(auth.require_teacher)):
    """
    Alunni di UNA classe, in ordine alfabetico, con punteggio/fiamma "Topo da
    Biblioteca" e prestito attivo se presente. Accessibile solo se la classe
    è tra quelle assegnate al professore loggato (non si possono vedere
    alunni di classi non proprie).
    """
    import gamification
    teacher = db.query(models.Teacher).get(teacher_payload["teacher_id"])
    if not teacher:
        raise HTTPException(404, "Professore non trovato")

    class_ids = {c.id for c in teacher.classes}
    if class_id not in class_ids:
        raise HTTPException(403, "Questa classe non è tra le tue.")

    students = (
        db.query(models.Student)
        .filter_by(class_id=class_id, active=True)
        .order_by(models.Student.full_name)
        .all()
    )
    active_loans = {
        l.user: l for l in db.query(models.Loan).filter_by(returned=False).all()
    }

    result = []
    for s in students:
        score, _ = gamification.reset_if_new_year(s.score or 0, s.score_year)
        level = gamification.get_level_info(score)
        loan = active_loans.get(s.full_name)
        result.append({
            "id": s.id, "fullName": s.full_name,
            "score": score, "flames": level["flames"],
            "activeLoan": loan.book_title if loan else None,
        })
    return result


@app.get("/teacher/overdue-students", tags=["Portale Professori"])
def teacher_overdue_students(db: Session = Depends(get_db),
                               teacher_payload: dict = Depends(auth.require_teacher)):
    """
    Lista degli alunni in ritardo nelle classi del professore loggato, con
    eventuale nota già lasciata. Solo le classi assegnate a QUESTO professore
    — non vede alunni di classi non sue.
    """
    teacher = db.query(models.Teacher).get(teacher_payload["teacher_id"])
    if not teacher:
        raise HTTPException(404, "Professore non trovato")

    class_ids = [c.id for c in teacher.classes]
    if not class_ids:
        return []

    now = datetime.now(timezone.utc)
    overdue_loans = (
        db.query(models.Loan)
        .filter(models.Loan.returned == False, models.Loan.due_date < now)
        .all()
    )

    result = []
    for loan in overdue_loans:
        student = db.query(models.Student).filter_by(full_name=loan.user).first()
        if not student or student.class_id not in class_ids:
            continue
        days_late = (now - loan.due_date).days
        note = db.query(models.TeacherNote).filter_by(
            teacher_id=teacher.id, loan_id=loan.id,
        ).first()
        result.append({
            "loanId":     loan.id,
            "studentName": student.full_name,
            "className":  student.school_class.name if student.school_class else "—",
            "bookTitle":  loan.book_title,
            "dueDate":    loan.due_date.isoformat(),
            "daysLate":   days_late,
            "note":       note.to_dict() if note else None,
        })

    result.sort(key=lambda r: -r["daysLate"])
    return result


class TeacherNoteUpsert(BaseModel):
    note: Optional[str] = None
    notified: Optional[bool] = None


@app.put("/teacher/notes/{loan_id}", tags=["Portale Professori"])
def upsert_teacher_note(loan_id: str, body: TeacherNoteUpsert, db: Session = Depends(get_db),
                          teacher_payload: dict = Depends(auth.require_teacher)):
    """
    Crea o aggiorna la nota del professore su un prestito in ritardo (es.
    "ho avvisato l'alunno il 3/7"). Ogni professore ha la propria nota
    indipendente sullo stesso prestito (non condivisa tra colleghi).
    """
    loan = db.query(models.Loan).get(loan_id)
    if not loan:
        raise HTTPException(404, "Prestito non trovato")

    teacher_id = teacher_payload["teacher_id"]
    note = db.query(models.TeacherNote).filter_by(teacher_id=teacher_id, loan_id=loan_id).first()
    if not note:
        note = models.TeacherNote(teacher_id=teacher_id, loan_id=loan_id)
        db.add(note)

    if body.note is not None:
        note.note = body.note
    if body.notified is not None:
        note.notified = body.notified

    db.commit()
    db.refresh(note)
    return note.to_dict()


# ══════════════════════════════════════════════════════════════════════════════
# RECENSIONI LIBRI
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/books/{book_id}/reviews", tags=["Recensioni"])
def list_book_reviews(book_id: str, db: Session = Depends(get_db)):
    """Lista pubblica delle recensioni di un libro (visibile a tutti, anche senza login)."""
    reviews = (
        db.query(models.Review)
        .filter_by(book_id=book_id)
        .order_by(models.Review.created_at.desc())
        .all()
    )
    avg = round(sum(r.rating for r in reviews) / len(reviews), 1) if reviews else None
    return {"reviews": [r.to_dict() for r in reviews], "averageRating": avg, "count": len(reviews)}


@app.post("/reviews", tags=["Recensioni"])
def create_review(body: ReviewCreate, db: Session = Depends(get_db),
                    student_payload: dict = Depends(auth.require_student)):
    """
    Crea (o aggiorna, se già esistente) la recensione dello studente loggato
    per un libro. Richiede che lo studente abbia effettivamente avuto in
    prestito quel libro almeno una volta (anti-abuso: non si recensiscono
    libri mai letti tramite questo sistema).
    """
    full_name = student_payload["name"]
    had_loan = db.query(models.Loan).filter_by(user=full_name, book_id=body.book_id).first()
    if not had_loan:
        raise HTTPException(400, "Puoi recensire solo libri che hai avuto in prestito.")

    book = db.query(models.Book).get(body.book_id)
    if not book:
        raise HTTPException(404, "Libro non trovato")

    existing = db.query(models.Review).filter_by(student_name=full_name, book_id=body.book_id).first()
    if existing:
        existing.rating = body.rating
        existing.text = body.text
        existing.loan_id = body.loan_id or existing.loan_id
        db.commit()
        db.refresh(existing)
        return existing.to_dict()

    review = models.Review(
        student_name=full_name, book_id=body.book_id,
        loan_id=body.loan_id, rating=body.rating, text=body.text,
    )
    db.add(review)
    db.commit()
    db.refresh(review)
    return review.to_dict()


@app.get("/reviews/me", tags=["Recensioni"])
def my_reviews(db: Session = Depends(get_db),
                student_payload: dict = Depends(auth.require_student)):
    """Tutte le recensioni lasciate dallo studente loggato."""
    reviews = (
        db.query(models.Review)
        .filter_by(student_name=student_payload["name"])
        .order_by(models.Review.created_at.desc())
        .all()
    )
    return [r.to_dict() for r in reviews]


@app.delete("/reviews/{review_id}", tags=["Recensioni"])
def delete_review(review_id: str, db: Session = Depends(get_db),
                    student_payload: dict = Depends(auth.require_student)):
    """Elimina una propria recensione."""
    review = db.query(models.Review).get(review_id)
    if not review or review.student_name != student_payload["name"]:
        raise HTTPException(404, "Recensione non trovata")
    db.delete(review)
    db.commit()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# CONSIGLI DI LETTURA PER CLASSE (dal professore)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/classes/{class_id}/recommendation", tags=["Consigli di lettura"])
def get_class_recommendation(class_id: str, db: Session = Depends(get_db)):
    """
    Consigli di lettura attivi per una classe (pubblico, senza login: serve
    al frontend studente per mostrare il consiglio del prof nella ricerca).
    Una classe può avere più consigli se più professori ne hanno impostato uno.
    """
    recs = db.query(models.ClassRecommendation).filter_by(class_id=class_id).all()
    return [r.to_dict() for r in recs]


@app.put("/teacher/recommendations", tags=["Portale Professori"])
def set_class_recommendation(body: ClassRecommendationCreate, db: Session = Depends(get_db),
                               teacher_payload: dict = Depends(auth.require_teacher)):
    """
    Crea o aggiorna il consiglio di lettura del professore loggato per UNA
    delle sue classi. Un professore può consigliare un genere diverso per
    ciascuna classe a cui è assegnato; non può impostare consigli per classi
    che non sono sue.
    """
    teacher_id = teacher_payload["teacher_id"]
    teacher = db.query(models.Teacher).get(teacher_id)
    if not teacher:
        raise HTTPException(404, "Professore non trovato")

    class_ids = {c.id for c in teacher.classes}
    if body.class_id not in class_ids:
        raise HTTPException(403, "Puoi consigliare libri solo per le tue classi.")

    if body.category not in genre_taxonomy.TAXONOMY:
        raise HTTPException(400, "Categoria non valida.")

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=CLASS_RECOMMENDATION_DAYS)

    existing = db.query(models.ClassRecommendation).filter_by(
        teacher_id=teacher_id, class_id=body.class_id,
    ).first()
    if existing:
        existing.category = body.category
        existing.subcategory = body.subcategory
        existing.note = body.note
        existing.expires_at = expires_at
        db.commit()
        db.refresh(existing)
        return existing.to_dict()

    rec = models.ClassRecommendation(
        teacher_id=teacher_id, class_id=body.class_id,
        category=body.category, subcategory=body.subcategory, note=body.note,
        expires_at=expires_at,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec.to_dict()


@app.delete("/teacher/recommendations/{class_id}", tags=["Portale Professori"])
def delete_class_recommendation(class_id: str, db: Session = Depends(get_db),
                                  teacher_payload: dict = Depends(auth.require_teacher)):
    """Rimuove il proprio consiglio di lettura per una classe."""
    teacher_id = teacher_payload["teacher_id"]
    rec = db.query(models.ClassRecommendation).filter_by(
        teacher_id=teacher_id, class_id=class_id,
    ).first()
    if not rec:
        raise HTTPException(404, "Nessun consiglio trovato")
    db.delete(rec)
    db.commit()
    return {"ok": True}


@app.get("/teacher/my-recommendations", tags=["Portale Professori"])
def teacher_my_recommendations(db: Session = Depends(get_db),
                                 teacher_payload: dict = Depends(auth.require_teacher)):
    """Tutti i consigli di lettura impostati dal professore loggato, per le sue classi."""
    recs = db.query(models.ClassRecommendation).filter_by(
        teacher_id=teacher_payload["teacher_id"],
    ).all()
    return [r.to_dict() for r in recs]


# ══════════════════════════════════════════════════════════════════════════════
# SUGGERIMENTI PERSONALIZZATI
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/books/suggestions/me", tags=["Suggerimenti"])
def my_suggestions(db: Session = Depends(get_db),
                     student_payload: dict = Depends(auth.require_student)):
    """
    Suggerisce libri allo studente loggato in base alle categorie dei libri
    che ha già letto e valutato bene (4-5 stelle nelle recensioni, oppure
    semplicemente già presi in prestito se non ci sono ancora recensioni).
    Esclude libri già letti e quelli attualmente in prestito ad altri.
    Restituisce al massimo 10 suggerimenti.
    """
    full_name = student_payload["name"]

    # Libri già letti (storico prestiti, anche attivi) — da escludere dai suggerimenti
    read_loans = db.query(models.Loan).filter_by(user=full_name).all()
    read_book_ids = {l.book_id for l in read_loans}

    # Recensioni con voto alto: usate per pesare le categorie preferite
    good_reviews = (
        db.query(models.Review)
        .filter(models.Review.student_name == full_name, models.Review.rating >= 4)
        .all()
    )
    # Se non ci sono recensioni alte, usa comunque tutti i libri letti come base
    source_book_ids = {r.book_id for r in good_reviews} or read_book_ids
    if not source_book_ids:
        return []  # nessuno storico su cui basare suggerimenti

    source_books = db.query(models.Book).filter(models.Book.id.in_(source_book_ids)).all()
    preferred_categories = set()
    for b in source_books:
        for cat, _ in genre_taxonomy.classify_genre(b.genre or ""):
            if cat != genre_taxonomy.FALLBACK_CATEGORY:
                preferred_categories.add(cat)

    if not preferred_categories:
        return []

    taken_ids = {
        l.book_id for l in db.query(models.Loan.book_id).filter_by(returned=False).all()
    }

    candidates = db.query(models.Book).filter(~models.Book.id.in_(read_book_ids)).all()
    suggestions = []
    for b in candidates:
        matches = genre_taxonomy.classify_genre(b.genre or "")
        cats = {c for c, _ in matches}
        overlap = cats & preferred_categories
        if overlap:
            d = b.to_dict()
            d["available"] = b.id not in taken_ids
            d["matchedCategories"] = sorted(overlap)
            suggestions.append(d)

    # Privilegia i disponibili, poi per numero di categorie in comune
    suggestions.sort(key=lambda s: (not s["available"], -len(s["matchedCategories"])))
    return suggestions[:10]


# ══════════════════════════════════════════════════════════════════════════════
# BADGE / TRAGUARDI
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/students/me/badges", tags=["Gamification"])
def my_badges(db: Session = Depends(get_db),
               student_payload: dict = Depends(auth.require_student)):
    """
    Calcola e restituisce i badge dello studente loggato per l'anno
    scolastico corrente. Eventuali nuovi badge guadagnati vengono salvati
    automaticamente (idempotente: ricalcolare non duplica nulla).
    """
    import gamification
    full_name = student_payload["name"]
    current_year = gamification.get_school_year()

    # Prestiti restituiti nell'anno scolastico corrente
    all_returned = (
        db.query(models.Loan)
        .filter_by(user=full_name, returned=True)
        .all()
    )
    loans_this_year = [
        {"bookId": l.book_id, "dueDate": l.due_date, "returnDate": l.return_date}
        for l in all_returned
        if l.return_date and gamification.get_school_year(l.return_date) == current_year
    ]

    book_ids = {l["bookId"] for l in loans_this_year}
    books = db.query(models.Book).filter(models.Book.id.in_(book_ids)).all() if book_ids else []
    book_genres = {b.id: (b.genre or "") for b in books}
    book_authors = {b.id: b.author for b in books}

    review_count = db.query(models.Review).filter_by(student_name=full_name).count()

    earned_keys = badges.compute_earned_badges(loans_this_year, review_count, book_genres, book_authors)

    # Salva i nuovi badge guadagnati (idempotente grazie al vincolo unique)
    existing_keys = {
        b.badge_key for b in db.query(models.StudentBadge).filter_by(
            student_name=full_name, school_year=current_year,
        ).all()
    }
    new_keys = earned_keys - existing_keys
    for key in new_keys:
        db.add(models.StudentBadge(student_name=full_name, badge_key=key, school_year=current_year))
    if new_keys:
        db.commit()

        # Notifica interna + email per ogni nuovo badge sbloccato (G2).
        student = db.query(models.Student).filter_by(full_name=full_name).first()
        for key in new_keys:
            info = badges.BADGES.get(key)
            if not info:
                continue
            create_notification(
                db, "student", full_name, "badge_earned",
                title=f"{info['icon']} Nuovo traguardo: {info['title']}",
                body=info["description"],
            )
            if student and student.email:
                try:
                    email_service.send_badge_earned(
                        full_name, student.email, info["icon"], info["title"], info["description"],
                    )
                except Exception:
                    logger.exception("Errore invio email badge a %s", full_name)

    all_badges_this_year = (
        db.query(models.StudentBadge)
        .filter_by(student_name=full_name, school_year=current_year)
        .all()
    )

    return [
        {**badges.BADGES[b.badge_key], "key": b.badge_key, "earnedAt": b.earned_at.isoformat()}
        for b in all_badges_this_year if b.badge_key in badges.BADGES
    ]


# ══════════════════════════════════════════════════════════════════════════════
# STATISTICHE DI CLASSE (per il professore)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/teacher/classes/{class_id}/stats", tags=["Portale Professori"])
def teacher_class_stats(class_id: str, db: Session = Depends(get_db),
                          teacher_payload: dict = Depends(auth.require_teacher)):
    """
    Statistiche di lettura per UNA classe del professore loggato: libri letti
    nell'anno scolastico corrente, generi più popolari, alunni più attivi.
    Accessibile solo se la classe è tra quelle del professore.
    """
    import gamification
    teacher = db.query(models.Teacher).get(teacher_payload["teacher_id"])
    if not teacher:
        raise HTTPException(404, "Professore non trovato")
    class_ids = {c.id for c in teacher.classes}
    if class_id not in class_ids:
        raise HTTPException(403, "Questa classe non è tra le tue.")

    students = db.query(models.Student).filter_by(class_id=class_id).all()
    student_names = [s.full_name for s in students]
    if not student_names:
        return {"totalBooksRead": 0, "topGenres": [], "topReaders": []}

    current_year = gamification.get_school_year()
    returned_loans = (
        db.query(models.Loan)
        .filter(models.Loan.user.in_(student_names), models.Loan.returned == True)
        .all()
    )
    loans_this_year = [
        l for l in returned_loans
        if l.return_date and gamification.get_school_year(l.return_date) == current_year
    ]

    total_books_read = len(loans_this_year)

    # Generi più popolari (categorie della taxonomy)
    from collections import Counter
    book_ids = {l.book_id for l in loans_this_year}
    books = db.query(models.Book).filter(models.Book.id.in_(book_ids)).all() if book_ids else []
    genre_by_book = {b.id: (b.genre or "") for b in books}
    category_counter = Counter()
    for l in loans_this_year:
        for cat, _ in genre_taxonomy.classify_genre(genre_by_book.get(l.book_id, "")):
            if cat != genre_taxonomy.FALLBACK_CATEGORY:
                category_counter[cat] += 1
    top_genres = [{"category": c, "count": n} for c, n in category_counter.most_common(5)]

    # Alunni più attivi (per numero di libri restituiti nell'anno)
    reader_counter = Counter(l.user for l in loans_this_year)
    top_readers = [
        {"studentName": name, "booksRead": count}
        for name, count in reader_counter.most_common(10)
    ]

    return {
        "totalBooksRead": total_books_read,
        "studentCount": len(student_names),
        "topGenres": top_genres,
        "topReaders": top_readers,
    }
