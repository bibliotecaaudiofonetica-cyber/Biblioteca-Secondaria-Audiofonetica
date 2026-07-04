"""
Badge / traguardi — riconoscimenti extra rispetto al punteggio "Topo da
Biblioteca". Non influenzano il punteggio: sono solo motivazionali, pensati
per premiare comportamenti diversi (esplorare generi, scrivere recensioni,
essere puntuali) oltre alla semplice quantità di letture.

Si resettano ogni anno scolastico insieme al punteggio (vedi gamification.py
per la definizione di anno scolastico).

Questo modulo NON tocca il database: calcola solo, a partire dai dati già
disponibili (prestiti restituiti dell'anno corrente, recensioni), quali
badge lo studente ha guadagnato. È il chiamante (main.py) che salva i nuovi
badge ottenuti.
"""

BADGES = {
    "bookworm": {
        "icon": "🐛", "title": "Bookworm",
        "description": "Hai restituito almeno 3 libri in tempo o in anticipo",
    },
    "explorer": {
        "icon": "🗺️", "title": "Esploratore",
        "description": "Hai letto libri di almeno 5 categorie diverse",
    },
    "same_author": {
        "icon": "✍️", "title": "Fan di un autore",
        "description": "Hai letto almeno 3 libri dello stesso autore",
    },
    "critic": {
        "icon": "⭐", "title": "Critico letterario",
        "description": "Hai lasciato almeno 5 recensioni",
    },
    "speed_reader": {
        "icon": "⚡", "title": "Lettore veloce",
        "description": "Hai restituito un libro almeno 3 giorni prima della scadenza",
    },
    "perfectionist": {
        "icon": "🏆", "title": "Perfezionista",
        "description": "Hai restituito almeno 5 libri di fila, tutti in tempo o in anticipo",
    },
}


def compute_earned_badges(returned_loans_this_year: list, review_count: int,
                            book_genres: dict, book_authors: dict) -> set:
    """
    Calcola quali badge lo studente ha guadagnato, dati:
      - returned_loans_this_year: lista di prestiti restituiti nell'anno
        scolastico corrente, ciascuno un dict {bookId, dueDate, returnDate}
        (datetime già parsati)
      - review_count: numero di recensioni lasciate dallo studente
      - book_genres: dict {bookId: genere_testo} per i libri di
        returned_loans_this_year
      - book_authors: dict {bookId: autore} per gli stessi libri

    Restituisce un set di badge_key guadagnati (può essere vuoto).
    """
    earned = set()
    if not returned_loans_this_year:
        on_time_or_early = []
    else:
        on_time_or_early = [
            l for l in returned_loans_this_year
            if l["returnDate"] and l["returnDate"] <= l["dueDate"]
        ]

    # Bookworm: almeno 3 restituiti in tempo o anticipo
    if len(on_time_or_early) >= 3:
        earned.add("bookworm")

    # Esploratore: almeno 5 categorie diverse tra i libri letti
    import genre_taxonomy
    categories_seen = set()
    for loan in returned_loans_this_year:
        genre_text = book_genres.get(loan["bookId"], "")
        matches = genre_taxonomy.classify_genre(genre_text)
        for cat, _ in matches:
            categories_seen.add(cat)
    if len(categories_seen) >= 5:
        earned.add("explorer")

    # Fan di un autore: almeno 3 libri dello stesso autore
    from collections import Counter
    author_counts = Counter(
        book_authors.get(loan["bookId"], "") for loan in returned_loans_this_year
        if book_authors.get(loan["bookId"])
    )
    if author_counts and max(author_counts.values()) >= 3:
        earned.add("same_author")

    # Critico letterario: almeno 5 recensioni
    if review_count >= 5:
        earned.add("critic")

    # Lettore veloce: almeno un prestito restituito 3+ giorni prima
    for loan in returned_loans_this_year:
        if loan["returnDate"] and (loan["dueDate"] - loan["returnDate"]).total_seconds() >= 3 * 86400:
            earned.add("speed_reader")
            break

    # Perfezionista: almeno 5 restituiti di fila tutti in tempo/anticipo
    # (guardiamo gli ultimi N prestiti in ordine cronologico di restituzione)
    sorted_loans = sorted(
        [l for l in returned_loans_this_year if l["returnDate"]],
        key=lambda l: l["returnDate"],
    )
    streak = 0
    best_streak = 0
    for loan in sorted_loans:
        if loan["returnDate"] <= loan["dueDate"]:
            streak += 1
            best_streak = max(best_streak, streak)
        else:
            streak = 0
    if best_streak >= 5:
        earned.add("perfectionist")

    return earned
