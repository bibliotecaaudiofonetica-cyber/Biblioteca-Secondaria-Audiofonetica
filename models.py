from sqlalchemy import Column, String, Boolean, DateTime, ForeignKey, Text, Integer, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
import uuid
import random

from database import Base


def new_id() -> str:
    return str(uuid.uuid4())


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ══════════════════════════════════════════════════════════════════════════════
# CLASSI E PROFESSORI
# ══════════════════════════════════════════════════════════════════════════════

class SchoolClass(Base):
    """Una classe scolastica (es. '3B'). Ogni alunno appartiene a esattamente una classe."""
    __tablename__ = "classes"

    id          = Column(String, primary_key=True, default=new_id)
    name        = Column(String, nullable=False, unique=True, index=True)  # es. "3B"
    school_year = Column(String, default="")  # es. "2025/2026" (libero, facoltativo)
    created_at  = Column(DateTime(timezone=True), default=now_utc)

    students = relationship("Student", back_populates="school_class")
    teachers = relationship("Teacher", secondary="teacher_classes", back_populates="classes")

    def to_dict(self):
        return {
            "id":          self.id,
            "name":        self.name,
            "schoolYear":  self.school_year,
            "createdAt":   self.created_at.isoformat() if self.created_at else None,
            "studentCount": len(self.students) if self.students is not None else 0,
        }


class Teacher(Base):
    """
    Professore/referente di una o più classi. Accede al portale notifiche
    con nome+cognome (identificativo univoco) + password scelta al primo
    accesso. Nessuna email: stesso principio degli alunni.
    """
    __tablename__ = "teachers"

    id            = Column(String, primary_key=True, default=new_id)
    first_name    = Column(String, nullable=False)
    last_name     = Column(String, nullable=False)
    full_name     = Column(String, nullable=False, unique=True, index=True)  # identificativo di login
    email         = Column(String, nullable=True)   # impostata dal prof al primo accesso
    password_hash = Column(String, nullable=True)  # null finché non sceglie la password al primo accesso
    active        = Column(Boolean, default=True, index=True)
    created_at    = Column(DateTime(timezone=True), default=now_utc)

    classes = relationship("SchoolClass", secondary="teacher_classes", back_populates="teachers")
    recommendations = relationship("ClassRecommendation", back_populates="teacher", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id":         self.id,
            "firstName":  self.first_name,
            "lastName":   self.last_name,
            "fullName":   self.full_name,
            "email":      self.email,
            "active":     self.active,
            "createdAt":  self.created_at.isoformat() if self.created_at else None,
            "classIds":   [c.id for c in self.classes],
            "classNames": [c.name for c in self.classes],
            "passwordSet": self.password_hash is not None,
        }


class TeacherClass(Base):
    """Tabella ponte molti-a-molti tra professori e classi."""
    __tablename__ = "teacher_classes"

    teacher_id = Column(String, ForeignKey("teachers.id", ondelete="CASCADE"), primary_key=True)
    class_id   = Column(String, ForeignKey("classes.id", ondelete="CASCADE"), primary_key=True)


# ══════════════════════════════════════════════════════════════════════════════
# LIBRI / PRESTITI / LISTA D'ATTESA
# ══════════════════════════════════════════════════════════════════════════════

class Book(Base):
    __tablename__ = "books"

    id        = Column(String, primary_key=True, default=new_id)
    title     = Column(String, nullable=False, index=True)
    author    = Column(String, nullable=False, index=True)
    publisher = Column(String, default="")
    location  = Column(String, nullable=False)
    genre     = Column(String, default="", index=True)
    cover_url            = Column(String, nullable=True)
    openlibrary_checked  = Column(Boolean, default=False, nullable=False, server_default='0')

    loans     = relationship("Loan", back_populates="book", cascade="all, delete-orphan")
    waitlist  = relationship("Waitlist", back_populates="book", cascade="all, delete-orphan")

    def to_dict(self):
        active_loan = next((l for l in self.loans if not l.returned), None)
        return {
            "id":        self.id,
            "title":     self.title,
            "author":    self.author,
            "publisher": self.publisher,
            "location":  self.location,
            "genre":     self.genre,
            "coverUrl":           self.cover_url,
            "openlibraryChecked": self.openlibrary_checked,
            "available": active_loan is None,
        }


class Loan(Base):
    __tablename__ = "loans"

    id          = Column(String, primary_key=True, default=new_id)
    user        = Column(String, nullable=False, index=True)  # nome completo alunno (full_name)
    book_id     = Column(String, ForeignKey("books.id"), nullable=False)
    book_title  = Column(String, nullable=False)
    taken_date  = Column(DateTime(timezone=True), default=now_utc)
    due_date    = Column(DateTime(timezone=True), nullable=False)
    returned    = Column(Boolean, default=False, index=True)
    return_date = Column(DateTime(timezone=True), nullable=True)

    # Tracciamento notifiche email (per non spammare): data dell'ultimo promemoria
    # inviato all'alunno e dell'ultima notifica di ritardo inviata al/ai prof.
    last_student_reminder_at = Column(DateTime(timezone=True), nullable=True)
    last_teacher_alert_at    = Column(DateTime(timezone=True), nullable=True)
    # Tiene traccia di QUALE soglia di alert al professore è già stata
    # notificata per questo prestito: 0 = nessuna, 1 = inviato il primo
    # alert (TEACHER_FIRST_ALERT_DAYS), 2 = inviato anche il richiamo
    # (TEACHER_SECOND_ALERT_DAYS). Più chiaro e robusto che dedurlo da un
    # confronto di date/timestamp.
    teacher_alert_stage      = Column(Integer, default=0, nullable=False, server_default='0')

    book = relationship("Book", back_populates="loans")

    def to_dict(self):
        return {
            "id":          self.id,
            "user":        self.user,
            "bookId":      self.book_id,
            "bookTitle":   self.book_title,
            "takenDate":   self.taken_date.isoformat() if self.taken_date else None,
            "dueDate":     self.due_date.isoformat() if self.due_date else None,
            "returned":    self.returned,
            "returnDate":  self.return_date.isoformat() if self.return_date else None,
        }


class Waitlist(Base):
    __tablename__ = "waitlist"

    id      = Column(String, primary_key=True, default=new_id)
    book_id = Column(String, ForeignKey("books.id"), nullable=False)
    user    = Column(String, nullable=False)
    date    = Column(DateTime(timezone=True), default=now_utc)

    book = relationship("Book", back_populates="waitlist")

    def to_dict(self):
        return {
            "id":     self.id,
            "bookId": self.book_id,
            "user":   self.user,
            "date":   self.date.isoformat() if self.date else None,
        }


# ══════════════════════════════════════════════════════════════════════════════
# ALUNNI
# ══════════════════════════════════════════════════════════════════════════════

def generate_pin() -> str:
    return str(random.randint(10000, 99999))


class Student(Base):
    __tablename__ = "students"

    id         = Column(String, primary_key=True, default=new_id)
    full_name  = Column(String, nullable=False, unique=True, index=True)
    pin        = Column(String, nullable=False, default=generate_pin)
    active     = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime(timezone=True), default=now_utc)
    notes      = Column(String, default="")

    # Ogni alunno appartiene obbligatoriamente a una classe
    class_id   = Column(String, ForeignKey("classes.id"), nullable=True)  # nullable a livello DB per
                                                                            # compatibilità migrazione,
                                                                            # ma l'API impone sempre il valore
    school_class = relationship("SchoolClass", back_populates="students")

    # ── Sistema "Topo da Biblioteca" (punti/livelli) ──────────────────────────
    # Punteggio 0-100, valido per l'anno scolastico corrente (score_year, es.
    # "2026/2027"). Quando l'anno scolastico cambia, il punteggio si resetta
    # a 0 automaticamente alla prima azione registrata nel nuovo anno.
    score                    = Column(Integer, default=0, nullable=False, server_default='0')
    score_year               = Column(String, nullable=True)
    last_activity_at         = Column(DateTime(timezone=True), nullable=True)  # ultimo prestito/restituzione
    last_inactivity_penalty_at = Column(DateTime(timezone=True), nullable=True)  # ultima volta che è stata
                                                                                   # applicata la penalità da
                                                                                   # inattività (evita doppi conteggi)

    def to_dict(self):
        return {
            "id":        self.id,
            "fullName":  self.full_name,
            "pin":       self.pin,
            "email":     self.email,
            "active":    self.active,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "notes":     self.notes,
            "classId":   self.class_id,
            "className": self.school_class.name if self.school_class else None,
            "score":     self.score or 0,
        }


class Setting(Base):
    """Tabella chiave-valore per configurazioni runtime."""
    __tablename__ = "settings"

    key   = Column(String, primary_key=True)
    value = Column(String, nullable=False)


# ══════════════════════════════════════════════════════════════════════════════
# NOTIFICHE INTERNE (sostituiscono l'invio email)
# ══════════════════════════════════════════════════════════════════════════════

class Notification(Base):
    """
    Notifica mostrata nel portale (non via email). Il destinatario è uno
    studente (recipient_type='student', recipient_id = Student.full_name,
    usato come chiave perché è univoco) oppure un professore
    (recipient_type='teacher', recipient_id = Teacher.id).
    """
    __tablename__ = "notifications"

    id             = Column(String, primary_key=True, default=new_id)
    recipient_type = Column(String, nullable=False, index=True)  # "student" | "teacher"
    recipient_id   = Column(String, nullable=False, index=True)  # full_name per studenti, id per prof
    kind           = Column(String, nullable=False)  # "loan_confirm" | "due_soon" | "overdue" | "teacher_alert"
    title          = Column(String, nullable=False)
    body           = Column(String, default="")
    related_loan_id = Column(String, nullable=True)
    read           = Column(Boolean, default=False, nullable=False, index=True)
    created_at     = Column(DateTime(timezone=True), default=now_utc)

    def to_dict(self):
        return {
            "id":          self.id,
            "kind":        self.kind,
            "title":       self.title,
            "body":        self.body,
            "relatedLoanId": self.related_loan_id,
            "read":        self.read,
            "createdAt":   self.created_at.isoformat() if self.created_at else None,
        }


class TeacherNote(Base):
    """
    Nota lasciata da un professore su un prestito in ritardo di un suo alunno
    (es. "ho avvisato l'alunno il 3/7"). Tiene anche un flag "avvisato" per
    poter marcare rapidamente la situazione come gestita.
    """
    __tablename__ = "teacher_notes"

    id          = Column(String, primary_key=True, default=new_id)
    teacher_id  = Column(String, ForeignKey("teachers.id"), nullable=False, index=True)
    loan_id     = Column(String, ForeignKey("loans.id"), nullable=False, index=True)
    note        = Column(String, default="")
    notified    = Column(Boolean, default=False, nullable=False)  # "ho avvisato l'alunno"
    created_at  = Column(DateTime(timezone=True), default=now_utc)
    updated_at  = Column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)

    def to_dict(self):
        return {
            "id":        self.id,
            "teacherId": self.teacher_id,
            "loanId":    self.loan_id,
            "note":      self.note,
            "notified":  self.notified,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
        }


# ══════════════════════════════════════════════════════════════════════════════
# RECENSIONI LIBRI
# ══════════════════════════════════════════════════════════════════════════════

class Review(Base):
    """
    Recensione facoltativa di un alunno su un libro, lasciata dopo la
    restituzione (o in qualsiasi momento dal proprio profilo per un libro
    già letto). Voto 1-5 stelle + testo libero opzionale.
    """
    __tablename__ = "reviews"

    id          = Column(String, primary_key=True, default=new_id)
    student_name = Column(String, nullable=False, index=True)  # Student.full_name
    book_id     = Column(String, ForeignKey("books.id"), nullable=False, index=True)
    loan_id     = Column(String, ForeignKey("loans.id"), nullable=True)  # prestito che ha originato la recensione
    rating      = Column(Integer, nullable=False)  # 1-5
    text        = Column(String, default="")
    created_at  = Column(DateTime(timezone=True), default=now_utc)

    book = relationship("Book")

    def to_dict(self):
        return {
            "id":          self.id,
            "studentName": self.student_name,
            "bookId":      self.book_id,
            "bookTitle":   self.book.title if self.book else None,
            "loanId":      self.loan_id,
            "rating":      self.rating,
            "text":        self.text,
            "createdAt":   self.created_at.isoformat() if self.created_at else None,
        }


# ══════════════════════════════════════════════════════════════════════════════
# CONSIGLI DI LETTURA PER CLASSE (dal professore)
# ══════════════════════════════════════════════════════════════════════════════

class ClassRecommendation(Base):
    """
    Genere/categoria consigliato da un professore per UNA delle sue classi
    (es. il prof di Lettere della 3B consiglia 'Avventura'). Visibile solo
    agli alunni di quella classe specifica nella ricerca a categorie.
    Un professore può avere al massimo un consiglio attivo per ciascuna
    classe (se ne imposta un altro, sovrascrive il precedente).

    Il consiglio scade automaticamente dopo 30 giorni (expires_at) e viene
    eliminato dallo scheduler giornaliero: nessun rinnovo automatico, se il
    prof lo vuole ancora attivo deve reimpostarlo da capo.
    """
    __tablename__ = "class_recommendations"
    __table_args__ = (UniqueConstraint("teacher_id", "class_id", name="uq_teacher_class_recommendation"),)

    id          = Column(String, primary_key=True, default=new_id)
    teacher_id  = Column(String, ForeignKey("teachers.id"), nullable=False, index=True)
    class_id    = Column(String, ForeignKey("classes.id"), nullable=False, index=True)
    category    = Column(String, nullable=False)     # categoria della taxonomy, es. "Avventura"
    subcategory = Column(String, nullable=True)       # sottocategoria opzionale
    note        = Column(String, default="")          # messaggio facoltativo del prof
    created_at  = Column(DateTime(timezone=True), default=now_utc)
    updated_at  = Column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)
    expires_at  = Column(DateTime(timezone=True), nullable=True)  # created_at + 30 giorni

    teacher = relationship("Teacher", back_populates="recommendations")
    school_class = relationship("SchoolClass")

    def to_dict(self):
        return {
            "id":          self.id,
            "teacherId":   self.teacher_id,
            "teacherName": self.teacher.full_name if self.teacher else None,
            "classId":     self.class_id,
            "className":   self.school_class.name if self.school_class else None,
            "category":    self.category,
            "subcategory": self.subcategory,
            "note":        self.note,
            "createdAt":   self.created_at.isoformat() if self.created_at else None,
            "updatedAt":   self.updated_at.isoformat() if self.updated_at else None,
            "expiresAt":   self.expires_at.isoformat() if self.expires_at else None,
        }


# ══════════════════════════════════════════════════════════════════════════════
# WISHLIST ("da leggere") — separata dalla lista d'attesa
# ══════════════════════════════════════════════════════════════════════════════

class Wishlist(Base):
    """
    Libri che uno studente vuole tenere d'occhio per dopo, indipendentemente
    dalla disponibilità. A differenza della Waitlist, non genera nessuna
    prenotazione automatica: è solo un promemoria personale.
    """
    __tablename__ = "wishlist"
    __table_args__ = (UniqueConstraint("student_name", "book_id", name="uq_wishlist_student_book"),)

    id           = Column(String, primary_key=True, default=new_id)
    student_name = Column(String, nullable=False, index=True)  # Student.full_name
    book_id      = Column(String, ForeignKey("books.id"), nullable=False, index=True)
    added_at     = Column(DateTime(timezone=True), default=now_utc)

    book = relationship("Book")

    def to_dict(self):
        return {
            "id":      self.id,
            "bookId":  self.book_id,
            "bookTitle": self.book.title if self.book else None,
            "bookAuthor": self.book.author if self.book else None,
            "addedAt": self.added_at.isoformat() if self.added_at else None,
        }


# ══════════════════════════════════════════════════════════════════════════════
# BADGE / TRAGUARDI
# ══════════════════════════════════════════════════════════════════════════════

class StudentBadge(Base):
    """
    Badge ottenuto da uno studente in un dato anno scolastico. Si resetta
    insieme al punteggio gamification ogni settembre: i badge dell'anno
    precedente restano in tabella (storico) ma non vengono più mostrati
    come "attivi" nell'anno corrente.
    """
    __tablename__ = "student_badges"
    __table_args__ = (UniqueConstraint("student_name", "badge_key", "school_year", name="uq_student_badge_year"),)

    id           = Column(String, primary_key=True, default=new_id)
    student_name = Column(String, nullable=False, index=True)
    badge_key    = Column(String, nullable=False)   # es. "bookworm", "explorer"...
    school_year  = Column(String, nullable=False, index=True)  # es. "2026/2027"
    earned_at    = Column(DateTime(timezone=True), default=now_utc)

    def to_dict(self):
        return {
            "id":         self.id,
            "badgeKey":   self.badge_key,
            "schoolYear": self.school_year,
            "earnedAt":   self.earned_at.isoformat() if self.earned_at else None,
        }
