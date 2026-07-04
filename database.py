"""
Configurazione database.

In produzione: imposta la variabile d'ambiente DATABASE_URL con la stringa
di connessione Postgres fornita da Neon, es:
  postgresql://utente:password@host.neon.tech/dbname?sslmode=require

In sviluppo locale: se DATABASE_URL non e' impostata, si usa automaticamente
un file SQLite locale (biblioteca.db) - comodo per provare senza configurare
nulla, ma NON va usato in produzione con piu' postazioni concorrenti.
"""

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

if DATABASE_URL:
    # Alcuni provider forniscono l'URL con prefisso "postgres://" (legacy);
    # SQLAlchemy 2.x richiede "postgresql://".
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
else:
    DATABASE_URL = f"sqlite:///{os.path.join(BASE_DIR, 'biblioteca.db')}"
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def is_postgres() -> bool:
    return DATABASE_URL.startswith("postgresql")
