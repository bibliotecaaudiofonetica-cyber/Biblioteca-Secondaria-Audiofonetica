#!/usr/bin/env python3
"""
Utilità per generare l'hash bcrypt della password admin.

Uso:
    python setup_admin.py

Poi copia le variabili d'ambiente nel file .env o nell'ambiente del server.
"""

import secrets
import sys

try:
    import bcrypt
except ImportError:
    print("Installa le dipendenze prima: pip install -r requirements.txt")
    sys.exit(1)


def main():
    print("=" * 50)
    print("  Setup password admin — Biblioteca Scolastica")
    print("=" * 50)

    password = input("\nInserisci la password admin: ").strip()
    if not password:
        print("Password vuota, uscita.")
        sys.exit(1)

    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    secret = secrets.token_hex(32)

    print("\n✅ Aggiungi queste variabili d'ambiente al tuo sistema o file .env:\n")
    print(f'BIBLIOTECA_SECRET="{secret}"')
    print(f'BIBLIOTECA_ADMIN_HASH="{hashed}"')
    print()
    print("Esempio con .env (richiede python-dotenv):")
    print("  Crea un file .env nella cartella backend/ con il contenuto sopra.")
    print("  Poi aggiungi all'inizio di main.py:")
    print('    from dotenv import load_dotenv; load_dotenv()')
    print()
    print("⚠️  Non committare mai il file .env nel repository!")
    print()
    print("Ricorda che servono anche queste altre variabili (vedi .env.example):")
    print("  GOOGLE_BOOKS_API_KEY, DATABASE_URL, BREVO_API_KEY, EMAIL_FROM,")
    print("  FRONTEND_URL")


if __name__ == "__main__":
    main()
