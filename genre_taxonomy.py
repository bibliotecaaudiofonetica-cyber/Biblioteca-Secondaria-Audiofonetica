"""
Taxonomia di generi a categorie/sottocategorie, per la ricerca a pulsanti.

Il catalogo reale ha centinaia di valori "genre" scritti a mano nel tempo
(es. "narrativa / formazione", "storico / guerra", "avventura / pirati"...).
Per offrire ai ragazzi una navigazione semplice a pulsanti (categoria →
sottocategoria), serve una taxonomia FISSA e curata, indipendente dal testo
libero in database. Ogni libro viene assegnato a una o più sottocategorie
tramite ricerca di parole chiave nel suo campo "genre" (case-insensitive).

Se un libro non genere non corrisponde a nessuna parola chiave, finisce
automaticamente nella categoria residuale "Scopri di tutto" — nessun libro
resta escluso dalla navigazione.
"""

# Ogni sottocategoria ha una lista di parole chiave: se almeno una compare
# nel campo "genre" del libro (case-insensitive), il libro entra in quella
# sottocategoria. Un libro può comparire in più sottocategorie/categorie.
TAXONOMY = {
    "Avventura": {
        "icon": "🗺️",
        "subcategories": {
            "Esplorazione & viaggi":     ["avventura", "viaggio", "esplorazione"],
            "Pirati & mare":             ["pirat", "mare", "marin"],
            "Sopravvivenza & natura":    ["sopravvivenza", "natura", "naturalistico"],
        },
    },
    "Fantasy & Magia": {
        "icon": "🐉",
        "subcategories": {
            "Mondi fantastici":          ["fantasy", "fantastico"],
            "Miti e leggende":           ["mitologia", "mito", "leggend", "epico", "cavalleresco"],
            "Fiabe":                     ["fiaba", "fiabe", "favola", "favole"],
        },
    },
    "Fantascienza": {
        "icon": "🚀",
        "subcategories": {
            "Futuro & spazio":           ["fantascienza", "distop"],
            "Robot & tecnologia":        ["robot", "tecnolog", "cyber"],
        },
    },
    "Giallo & Mistero": {
        "icon": "🔎",
        "subcategories": {
            "Indagini & gialli":         ["giallo", "investigat", "detective"],
            "Mistero":                   ["mister"],
            "Thriller":                  ["thriller", "spionaggio"],
            "Brividi & horror":          ["horror", "gotic", "paura"],
        },
    },
    "Storia & Memoria": {
        "icon": "🏛️",
        "subcategories": {
            "Storico":                   ["storic"],
            "Guerra & Resistenza":       ["guerra", "resistenza"],
            "Biografie":                 ["biograf"],
            "Testimonianze":             ["testimonianza", "memoria", "diario"],
        },
    },
    "Vita di tutti i giorni": {
        "icon": "🌍",
        "subcategories": {
            "Storie di vita":            ["narrativa", "romanzo", "novella", "verismo", "urbano"],
            "Crescere & cambiare":       ["formazione", "crescita", "giovani adulti"],
            "Amicizia & famiglia":       ["amicizia", "familiar", "sociale"],
            "Storie vere & realistiche": ["realistic", "contempora", "psicologic", "introspettiv"],
            "Sentimenti & emozioni":     ["sentimental", "romantic", "amore", "epistolare"],
            "Sport in pagina":           ["sport"],
        },
    },
    "Comico & Leggero": {
        "icon": "😄",
        "subcategories": {
            "Storie divertenti":         ["umoristic", "comic", "divertent"],
            "Racconti brevi":            ["racconti", "racconto", "novelle", "antologia"],
        },
    },
    "Teatro & Poesia": {
        "icon": "🎭",
        "subcategories": {
            "Teatro":                    ["teatro", "commedia", "dramma", "tragedia"],
            "Poesia":                    ["poesia", "poetic"],
        },
    },
    "Scopri & Imparare": {
        "icon": "🔬",
        "subcategories": {
            "Scienza & natura":          ["scientific", "divulgativo", "divulgazione", "scienza", "matematica", "geografic"],
            "Fumetti & illustrati":      ["fumetto", "fumetti", "illustrat", "supereroi"],
            "Religione & filosofia":     ["religios", "filosof", "biblic", "sapienzial", "agiografia"],
            "Guide & saggi":             ["saggio", "saggistica", "manuale", "didattico", "guida", "giornalismo", "dizionario"],
        },
    },
}

# Categoria/sottocategoria residuale per libri senza genere o non mappati
FALLBACK_CATEGORY = "Scopri di tutto"
FALLBACK_ICON = "📚"


def classify_genre(genre_text: str) -> list[tuple[str, str]]:
    """
    Dato il testo libero del campo "genre" di un libro, restituisce la lista
    di tuple (categoria, sottocategoria) in cui il libro deve apparire.
    Un libro può comparire in più sottocategorie se il testo contiene più
    parole chiave di categorie diverse.
    """
    if not genre_text or not genre_text.strip():
        return [(FALLBACK_CATEGORY, FALLBACK_CATEGORY)]

    text = genre_text.lower()
    matches = []
    for category, data in TAXONOMY.items():
        for subcat, keywords in data["subcategories"].items():
            if any(kw in text for kw in keywords):
                matches.append((category, subcat))

    if not matches:
        return [(FALLBACK_CATEGORY, FALLBACK_CATEGORY)]
    return matches


def get_taxonomy_tree() -> dict:
    """
    Restituisce l'albero categoria -> [sottocategorie] con icone, nel formato
    pronto per il frontend (pulsanti a cascata). Include sempre la categoria
    residuale in coda.
    """
    tree = {}
    for category, data in TAXONOMY.items():
        tree[category] = {
            "icon": data["icon"],
            "subcategories": list(data["subcategories"].keys()),
        }
    tree[FALLBACK_CATEGORY] = {"icon": FALLBACK_ICON, "subcategories": [FALLBACK_CATEGORY]}
    return tree
