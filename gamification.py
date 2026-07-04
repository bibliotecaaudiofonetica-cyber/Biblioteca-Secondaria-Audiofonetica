"""
Sistema "Topo da Biblioteca" — punteggio 0-100 a challenge annuale.

Regole:
  - Ogni studente ha un punteggio (score) compreso tra 0 e 100, valido per
    l'anno scolastico corrente (campo score_year, es. "2026/2027").
  - Il punteggio si RESETTA a 0 automaticamente alla prima azione registrata
    in un nuovo anno scolastico (l'anno scolastico cambia il 1° settembre).
  - Si guadagnano punti restituendo libri (di più se in anticipo, meno se
    in ritardo). Si perdono punti per ogni giorno di ritardo e per lunghi
    periodi di inattività di lettura.
  - Il punteggio non scende mai sotto 0 né sale mai sopra 100.

Questo modulo NON tocca il database direttamente: le funzioni ricevono e
restituiscono solo numeri/stringhe, è il chiamante (main.py) a leggere e
salvare il valore aggiornato sull'oggetto Student.
"""

from datetime import datetime, timezone

# ── Punti guadagnati/perduti per azione ──────────────────────────────────────
POINTS_ON_TIME       = 8    # restituzione in tempo
POINTS_EARLY         = 12   # restituzione almeno EARLY_DAYS_THRESHOLD giorni prima della scadenza
POINTS_LATE          = 2    # restituzione in ritardo (comunque premiato il leggere)
EARLY_DAYS_THRESHOLD = 2

PENALTY_PER_LATE_DAY     = 1   # punti persi per ogni giorno di ritardo, applicati alla restituzione
INACTIVITY_PERIOD_DAYS   = 30  # ogni N giorni di inattività...
INACTIVITY_PENALTY       = 5   # ...si perdono questi punti

SCORE_MIN = 0
SCORE_MAX = 100

# ── Fasce "fiamma" e titolo (solo estetica, derivate dal punteggio) ─────────
LEVELS = [
    {"min": 0,  "max": 20,  "flames": 1, "title": "🐭 Topolino curioso"},
    {"min": 21, "max": 45,  "flames": 2, "title": "📖 Lettore in crescita"},
    {"min": 46, "max": 70,  "flames": 3, "title": "🔍 Topo da biblioteca"},
    {"min": 71, "max": 90,  "flames": 4, "title": "🦉 Gufo notturno"},
    {"min": 91, "max": 100, "flames": 5, "title": "👑 Leggenda della biblioteca"},
]


def get_school_year(when: datetime = None) -> str:
    """
    Restituisce l'anno scolastico corrente nel formato "2026/2027".
    L'anno scolastico cambia il 1° settembre: da settembre a dicembre è
    "anno/anno+1", da gennaio ad agosto è "anno-1/anno".
    """
    when = when or datetime.now(timezone.utc)
    if when.month >= 9:
        return f"{when.year}/{when.year + 1}"
    return f"{when.year - 1}/{when.year}"


def get_level_info(score: int) -> dict:
    """Restituisce {flames, title} per il punteggio dato."""
    score = max(SCORE_MIN, min(SCORE_MAX, score or 0))
    for level in LEVELS:
        if level["min"] <= score <= level["max"]:
            return {"flames": level["flames"], "title": level["title"]}
    return {"flames": 1, "title": LEVELS[0]["title"]}


def _clamp(score: int) -> int:
    return max(SCORE_MIN, min(SCORE_MAX, score))


def reset_if_new_year(current_score: int, current_score_year: str, now: datetime = None) -> tuple[int, str]:
    """
    Se l'anno scolastico è cambiato rispetto a current_score_year, restituisce
    (0, nuovo_anno). Altrimenti restituisce (current_score, current_score_year)
    inalterati. Va chiamata PRIMA di applicare qualsiasi punteggio/penalità.
    """
    now = now or datetime.now(timezone.utc)
    this_year = get_school_year(now)
    if current_score_year != this_year:
        return 0, this_year
    return (current_score or 0), current_score_year


def apply_return_points(score: int, score_year: str, due_date: datetime,
                          return_date: datetime = None) -> tuple[int, str, int, str]:
    """
    Calcola i punti da assegnare/sottrarre per una restituzione.
    Restituisce (nuovo_score, score_year, delta_punti, motivo) — il motivo è
    una stringa breve utile per log/notifiche ("on_time", "early", "late").
    """
    return_date = return_date or datetime.now(timezone.utc)
    score, score_year = reset_if_new_year(score, score_year, return_date)

    days_diff = (due_date - return_date).total_seconds() / 86400

    if days_diff >= EARLY_DAYS_THRESHOLD:
        delta = POINTS_EARLY
        reason = "early"
    elif days_diff >= 0:
        delta = POINTS_ON_TIME
        reason = "on_time"
    else:
        # Giorni di ritardo, arrotondati per eccesso solo sulla parte oltre il
        # giorno stesso della scadenza (es. 0.1 giorni di ritardo = 1 giorno,
        # 1.1 giorni di ritardo = 2 giorni), evitando di sommare un giorno extra.
        import math
        days_late = max(1, math.ceil(abs(days_diff)))
        delta = POINTS_LATE - (PENALTY_PER_LATE_DAY * days_late)
        reason = "late"

    new_score = _clamp(score + delta)
    return new_score, score_year, delta, reason


def apply_inactivity_penalty(score: int, score_year: str, last_activity_at: datetime,
                               last_penalty_at: datetime, now: datetime = None) -> tuple[int, str, datetime, bool]:
    """
    Se sono passati >= INACTIVITY_PERIOD_DAYS giorni dall'ultima attività (e
    dall'ultima penalità già applicata), applica -INACTIVITY_PENALTY punti.
    Restituisce (nuovo_score, score_year, nuovo_last_penalty_at, applicata).
    Va chiamata dallo scheduler giornaliero per ogni studente attivo.
    """
    now = now or datetime.now(timezone.utc)
    score, score_year = reset_if_new_year(score, score_year, now)

    if not last_activity_at:
        return score, score_year, last_penalty_at, False

    reference = last_penalty_at if (last_penalty_at and last_penalty_at > last_activity_at) else last_activity_at
    days_since = (now - reference).days

    if days_since >= INACTIVITY_PERIOD_DAYS:
        new_score = _clamp(score - INACTIVITY_PENALTY)
        return new_score, score_year, now, True

    return score, score_year, last_penalty_at, False
