# Guida al deploy online — Biblioteca Scolastica

Questa guida ti porta passo passo dalla situazione attuale (tutto su un PC della
biblioteca) a un sistema accessibile da internet, gratuito, da ogni PC scolastico.

Userai 4 servizi gratuiti, tutti senza carta di credito richiesta per i piani free:

| Servizio | A cosa serve | Costo |
|---|---|---|
| **Neon** | Database (sostituisce il file SQLite) | Gratis |
| **Render** | Esegue il backend (le API) | Gratis (con sleep) |
| **Cloudflare Pages** | Ospita il frontend (la pagina web) | Gratis |
| **Brevo** | Invia le email automatiche | Gratis (300/giorno) |

Tempo stimato per completare tutta la guida: 1-2 ore con calma.

---

## Indice

1. [Creare il database su Neon](#1-creare-il-database-su-neon)
2. [Creare l'account email su Brevo](#2-creare-laccount-email-su-brevo)
3. [Preparare i segreti dell'applicazione](#3-preparare-i-segreti-dellapplicazione)
4. [Pubblicare il backend su Render](#4-pubblicare-il-backend-su-render)
5. [Pubblicare il frontend su Cloudflare Pages](#5-pubblicare-il-frontend-su-cloudflare-pages)
6. [Primo avvio e popolamento dati](#6-primo-avvio-e-popolamento-dati)
7. [Collegare i PC della scuola](#7-collegare-i-pc-della-scuola)
8. [Test finale (checklist)](#8-test-finale-checklist)
9. [Manutenzione e domande frequenti](#9-manutenzione-e-domande-frequenti)

---

## 1. Creare il database su Neon

Neon sostituisce il vecchio file `biblioteca.db`. È un database Postgres
gratuito, già pronto per essere usato da più PC contemporaneamente senza
rischio di "database bloccato".

1. Vai su **https://neon.tech** e clicca **Sign up** (puoi accedere anche con
   un account Google/GitHub, più veloce).
2. Una volta dentro, clicca **Create a project**.
   - Nome progetto: `biblioteca-scolastica`
   - Regione: scegli una in Europa (es. `Europe (Frankfurt)`), per avere
     meno latenza dall'Italia.
3. Una volta creato il progetto, Neon mostra una **stringa di connessione**
   (Connection string), simile a questa:
   ```
   postgresql://nome_utente:password@ep-xxxxx.eu-central-1.aws.neon.tech/neondb?sslmode=require
   ```
4. **Copia questa stringa e salvala da parte** (in un file di testo, per
   ora) — ti servirà al passo 4. Non condividerla con nessuno: equivale
   alla password di accesso completo al database.

> 💡 Il piano gratuito di Neon include backup automatici e si "addormenta"
> dopo un periodo di inattività come Render, ma si risveglia automaticamente
> alla prima richiesta in pochi secondi: non serve nessuna azione da parte
> tua.

---

## 2. Creare l'account email su Brevo

1. Vai su **https://www.brevo.com** e clicca **Sign up free**.
2. Compila la registrazione con un'email a cui hai accesso (può essere la
   stessa che userai come mittente, vedi punto 4 — semplifica le cose).
3. Una volta dentro la dashboard:
   - Clicca sul tuo nome/organizzazione in alto a destra → **SMTP & API**.
   - Vai sulla sezione **API Keys** e clicca **Generate a new API key**.
   - Dai un nome alla chiave, es. `biblioteca-backend`.
   - **Copia la chiave generata e salvala da parte** (comincia con
     qualcosa come `xkeysib-...`) — comparirà una sola volta, se la perdi
     dovrai generarne una nuova. **Non condividerla con nessuno**, nemmeno
     in chat: equivale a una password.

4. **Verifica un mittente** (passo obbligatorio, senza questo Brevo blocca
   ogni invio):
   - Dashboard → cliccando sul tuo nome in alto a destra → **Settings** →
     **Senders, Domains, IPs** → tab **Senders** → **Add a sender**.
   - Inserisci un nome (es. "Biblioteca Scolastica") e un indirizzo email
     **che esiste davvero e a cui hai accesso** — può essere una qualsiasi
     email personale (Gmail, Outlook, ecc.), non serve un dominio proprio
     per questo passo.
   - Brevo manda un codice a 6 cifre a quell'indirizzo: aprilo e inserisci
     il codice nella dashboard per completare la verifica.
   - Da questo momento, **solo quell'indirizzo verificato** può essere usato
     come mittente delle email automatiche del sistema.

5. **Controlla il blocco IP** (causa comune di errori "unauthorized" anche
   con mittente verificato): Settings → **Security** → **Authorized IPs**.
   Dato che il backend girerà su Render con un IP che può cambiare ad ogni
   riavvio, la soluzione più semplice è cliccare **Deactivate blocking** per
   disattivare il controllo IP (riduce leggermente la sicurezza dell'account
   Brevo, ma evita blocchi imprevisti quando l'IP del server cambia).

**Valori da salvare per il passo 4 della prossima sezione:**
- `BREVO_API_KEY` = la chiave copiata al punto 3
- `EMAIL_FROM` = l'indirizzo email che hai verificato al punto 4 (deve
  corrispondere esattamente, altrimenti Brevo rifiuta l'invio)
- `EMAIL_FROM_NAME` = `Biblioteca Scolastica`

> 💡 Le email arriveranno con questo indirizzo come mittente reale. Se in
> futuro avrai un dominio tuo, potrai autenticarlo su Brevo (Settings →
> Senders, Domains, IPs → Domains) per un aspetto più professionale, ma
> non è necessario per far funzionare il sistema.

---

## 3. Preparare i segreti dell'applicazione

Sul tuo PC (quello che usi ora per la biblioteca), apri un terminale nella
cartella `backend` e genera due valori:

**3.1 — Il secret per i login (token JWT):**
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```
Copia il risultato (una lunga stringa di lettere e numeri). Questo sarà
`BIBLIOTECA_SECRET`.

**3.2 — L'hash della password admin:**
```bash
python setup_admin.py
```
Ti chiederà di scegliere la password che userai per accedere alla sezione
riservata online. Scrivila, confermala, e lo script ti mostrerà una riga
tipo:
```
BIBLIOTECA_ADMIN_HASH=$2b$12$....................................
```
Copia tutto il valore dopo `=`. Questo sarà `BIBLIOTECA_ADMIN_HASH`.

> 🔒 **Scegli una password admin robusta** (almeno 12 caratteri, lettere
> maiuscole/minuscole/numeri): da quando il sistema è online, chiunque su
> internet può tentare di indovinarla (anche se il sistema blocca dopo
> troppi tentativi sbagliati).

**3.3 — La chiave Google Books (se non l'hai già):**
Se vuoi continuare a usare la ricerca automatica delle copertine, serve una
API key di Google Books:
1. Vai su **https://console.cloud.google.com**
2. Crea un progetto (o usa uno esistente) → **APIs & Services** → **Library**
3. Cerca **Books API** → **Enable**
4. Vai su **Credentials** → **Create credentials** → **API key**
5. Copia la chiave generata.

Se preferisci saltare questo passo, le copertine semplicemente non verranno
trovate tramite Google Books (resterà solo Open Library come fallback,
funziona comunque ma trova meno copertine).

---

## 4. Pubblicare il backend su Render

1. Vai su **https://render.com** e registrati (puoi usare GitHub o email).
2. Il modo più semplice per caricare il codice è tramite **GitHub**:
   - Se non hai un account GitHub, creane uno gratis su **github.com**.
   - Crea un nuovo repository (es. `biblioteca-backend`), **privato** (così
     nessun altro può vedere il codice).
   - Carica dentro la cartella `backend` (tutti i file: `main.py`,
     `models.py`, `auth.py`, `database.py`, `email_service.py`,
     `requirements.txt`, `setup_admin.py`, `.env.example` — **MAI il file
     `.env` reale**, se per errore lo hai creato in locale).

   *(Se non hai mai usato GitHub, fammelo sapere e ti preparo i comandi
   esatti da copiare nel terminale per fare l'upload.)*

3. Su Render, clicca **New** → **Web Service**.
4. Collega il repository GitHub appena creato.
5. Configura il servizio:
   - **Name**: `biblioteca-backend` (diventerà parte dell'URL pubblico)
   - **Region**: Frankfurt (EU) o la più vicina all'Italia
   - **Branch**: `main`
   - **Root Directory**: lascialo vuoto se hai caricato solo la cartella
     `backend` come repository, altrimenti scrivi `backend`
   - **Runtime**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Instance Type**: **Free**
6. Scorri fino a **Environment Variables** e aggiungi una per una tutte
   queste (usando i valori che hai raccolto nei passi precedenti):

   | Nome variabile | Valore |
   |---|---|
   | `BIBLIOTECA_SECRET` | (dal passo 3.1) |
   | `BIBLIOTECA_ADMIN_HASH` | (dal passo 3.2) |
   | `DATABASE_URL` | (la stringa di Neon dal passo 1) |
   | `GOOGLE_BOOKS_API_KEY` | (dal passo 3.3, opzionale) |
   | `BREVO_API_KEY` | (dal passo 2) |
   | `EMAIL_FROM` | (dal passo 2) |
   | `EMAIL_FROM_NAME` | `Biblioteca Scolastica` |
   | `FRONTEND_URL` | lascialo vuoto per ora, lo aggiungerai al passo 5 |
   | `CORS_ORIGINS` | lascialo vuoto per ora, lo aggiungerai al passo 5 |

7. Clicca **Create Web Service**. Render comincerà a installare le
   dipendenze e avviare il server: richiede 2-5 minuti la prima volta.
8. Quando il log mostra qualcosa come `Application startup complete`,
   il backend è online. Render ti darà un URL pubblico tipo:
   ```
   https://biblioteca-backend.onrender.com
   ```
   **Salvalo**: ti servirà al passo successivo.

9. Verifica che funzioni visitando, dal browser:
   ```
   https://biblioteca-backend.onrender.com/health
   ```
   Dovresti vedere una risposta JSON con `"status": "ok"`.

> ⏱️ **Nota sullo sleep**: con il piano free, se il backend non riceve
> richieste per un po', Render lo "addormenta". La richiesta successiva lo
> risveglia automaticamente, ma ci mette 30-60 secondi extra la prima volta.
> Il frontend che ho preparato gestisce già questo caso mostrando "Il server
> si sta avviando, attendere…" e riprovando da solo.

---

## 5. Pubblicare il frontend su Cloudflare Pages

1. Vai su **https://pages.cloudflare.com** e registrati/accedi.
2. Clicca **Create a project** → **Upload assets** (così non serve nemmeno
   GitHub per il frontend, basta caricare i file).
3. Trascina dentro la cartella `frontend` (deve contenere `index.html` e
   `logo.png`).
4. Dai un nome al progetto, es. `biblioteca-scuola` → Cloudflare ti darà un
   URL tipo:
   ```
   https://biblioteca-scuola.pages.dev
   ```
5. **Salva questo URL.**

6. Torna su Render (passo 4) e aggiorna le variabili d'ambiente che avevi
   lasciato vuote:
   - `FRONTEND_URL` = `https://biblioteca-scuola.pages.dev`
   - `CORS_ORIGINS` = `https://biblioteca-scuola.pages.dev`

   Dopo averle salvate, Render farà un breve riavvio automatico del backend
   (qualche decina di secondi).

---

## 6. Primo avvio e popolamento dati

Ora che backend e frontend sono online, devi:

**6.1 — Caricare il catalogo libri esistente.**
Apri il frontend con il parametro che lo collega al backend (sostituisci
con i tuoi URL reali):
```
https://biblioteca-scuola.pages.dev/?api=https://biblioteca-backend.onrender.com
```
La prima visita salva questo collegamento nel browser: le visite successive
non avranno più bisogno del parametro `?api=...`.

**6.2 — Popolare il catalogo iniziale (se parti da zero).**
Il catalogo "seed" di base ora richiede login admin. Vai sulla pagina di
login admin (link "Accesso riservato" in basso nella schermata principale),
accedi con la password scelta al passo 3.2, poi — dato che il pulsante
`/seed` non è ancora visibile come bottone nell'interfaccia — fammi sapere
e aggiungo un bottone "Carica catalogo iniziale" nel pannello admin, oppure
te lo faccio chiamare una volta da terminale con questo comando (sostituendo
URL e password):
```bash
curl -X POST https://biblioteca-backend.onrender.com/auth/login \
  -H "Content-Type: application/json" \
  -d '{"password":"LA_TUA_PASSWORD_ADMIN"}'
```
questo restituisce un token; poi:
```bash
curl -X POST https://biblioteca-backend.onrender.com/seed \
  -H "Authorization: Bearer IL_TOKEN_RICEVUTO_SOPRA"
```

**6.3 — Migrare il catalogo reale (1331 libri) e gli alunni esistenti.**
Hai due strade:
- **Backup/Restore**: dal vecchio sistema locale, esporta il backup JSON
  (pulsante già presente in "Backup" nel pannello admin attuale), poi
  importalo nel nuovo sistema online con lo stesso pulsante "Importa
  backup". Questo riporta libri e prestiti, **non** alunni/classi (per i
  motivi di sicurezza spiegati prima).
- **Alunni**: andranno ricreati nel nuovo sistema (con classe assegnata),
  dato che il vecchio formato non aveva la classe come campo strutturato.
  Se mi fornisci l'elenco alunni per classe (anche dal PDF "Alunni 3B" che
  avevi), posso prepararti un file di importazione rapida invece di
  inserirli uno per uno a mano.

---

## 7. Collegare i PC della scuola

Su ogni PC della biblioteca (o ovunque si debba accedere al sistema):

1. Apri il browser e vai su:
   ```
   https://biblioteca-scuola.pages.dev/?api=https://biblioteca-backend.onrender.com
   ```
2. Aggiungi questa pagina ai **preferiti/segnalibri** del browser (così non
   serve ridigitare l'URL lungo ogni volta).
3. *(Opzionale)* Imposta questa pagina come **pagina iniziale** del browser
   se il PC è dedicato solo alla biblioteca.

Da questo momento, quel browser ricorderà l'indirizzo del backend
(salvato internamente) e basterà andare su `https://biblioteca-scuola.pages.dev`
anche senza il parametro `?api=...`.

---

## 8. Test finale (checklist)

Prima di considerare il sistema pronto per l'uso reale, verifica:

- [ ] `https://TUO-BACKEND.onrender.com/health` risponde `{"status":"ok"}`
- [ ] Il frontend si carica e mostra la schermata di login
- [ ] Login admin funziona con la password scelta
- [ ] Riesci a creare almeno una classe (es. "3B")
- [ ] Riesci a creare un professore con email e assegnarlo a quella classe
- [ ] Riesci a creare un alunno assegnato a quella classe
- [ ] Login studente (nome + PIN) funziona e mostra la richiesta email
      facoltativa al primo accesso
- [ ] Prendere un libro in prestito funziona e (se hai messo l'email)
      arriva l'email di riepilogo
- [ ] Restituire un libro funziona
- [ ] Da un altro PC/telefono (rete diversa da quella di casa, es. dati
      mobili) il sistema è raggiungibile — verifica che davvero funzioni
      "da internet" e non solo dalla tua rete

Per testare l'invio email di ritardo senza aspettare giorni, puoi forzare
il controllo subito (richiede token admin, stesso procedimento del passo 6.2):
```bash
curl -X POST https://TUO-BACKEND.onrender.com/admin/check-overdue-now \
  -H "Authorization: Bearer IL_TUO_TOKEN_ADMIN"
```

---

## 9. Manutenzione e domande frequenti

**Come cambio la password admin più avanti?**
Rilancia `python setup_admin.py` in locale, copia il nuovo
`BIBLIOTECA_ADMIN_HASH` e aggiornalo nelle variabili d'ambiente su Render.

**Il backend è "andato in sleep", quanto aspetto?**
Di solito 30-60 secondi alla prima richiesta dopo un periodo di inattività.
Se vuoi eliminare del tutto questa attesa in futuro, l'unica strada è
passare a un piano Render a pagamento (~7€/mese) — per ora avevi scelto di
restare sul piano gratuito.

**Quante email posso inviare?**
Il piano gratuito di Brevo permette 300 email al giorno: per una biblioteca
scolastica è ampiamente sufficiente anche con tutte le notifiche attive.

**Posso vedere se le email vengono davvero consegnate?**
Sì: nella dashboard di Brevo, sezione **Transactional** → **Logs**, vedi
ogni email inviata e il suo stato (consegnata, rifiutata, in spam, ecc.).

**Cosa succede se Neon, Render o Brevo cambiano le condizioni del piano
gratuito in futuro?**
È un rischio intrinseco di qualsiasi servizio "gratis": nessuna garanzia è
eterna. Se succede, il passaggio a un piano a pagamento di base (in genere
pochi euro al mese per ciascun servizio) risolve senza dover cambiare
architettura.

**Posso aggiungere un dominio mio in futuro?**
Sì, in qualsiasi momento: sia Cloudflare Pages che Render supportano domini
personalizzati gratuitamente (paghi solo la registrazione del dominio,
8-15€/anno). Una volta fatto, potrai anche autenticare il dominio su Brevo
(Settings → Senders, Domains, IPs → Domains) per usare un indirizzo mittente
con il tuo dominio invece di una email personale.

**Le email non partono, dove controllo?**
Prima cosa: nei log di Render (dashboard → il tuo servizio → tab "Logs"),
cerca righe che iniziano con "Email NON inviata" — spiegano la causa esatta
(mittente non verificato, IP non autorizzato, ecc.). Seconda cosa: nella
dashboard Brevo, sezione **Transactional** → **Logs**, vedi lo stato di ogni
tentativo di invio.

---

*Guida preparata per il progetto Biblioteca Scolastica — versione con
Classi, Professori, notifiche email e deploy cloud gratuito.*
