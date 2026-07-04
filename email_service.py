"""
Servizio email tramite Brevo (ex Sendinblue) — Biblioteca Scolastica.

Variabili d'ambiente richieste:
  BREVO_API_KEY    chiave API Brevo (inizia con xkeysib-...)
  EMAIL_FROM       indirizzo mittente verificato su Brevo
  EMAIL_FROM_NAME  nome mittente (default: "Biblioteca Scolastica")

Se BREVO_API_KEY non è impostata, le funzioni loggano un avviso e
restituiscono False senza sollevare eccezioni — il sistema continua
a funzionare con le sole notifiche interne.
"""

import os
import logging
import httpx

logger = logging.getLogger("biblioteca.email")

BREVO_API_KEY   = os.environ.get("BREVO_API_KEY", "").strip()
EMAIL_FROM      = os.environ.get("EMAIL_FROM", "").strip()
EMAIL_FROM_NAME = os.environ.get("EMAIL_FROM_NAME", "Biblioteca Scolastica").strip()

BREVO_SEND_URL = "https://api.brevo.com/v3/smtp/email"


# ══════════════════════════════════════════════════════════════════════════════
# Helper di invio grezzo
# ══════════════════════════════════════════════════════════════════════════════

def send_email(to_email: str, to_name: str, subject: str, html_body: str) -> bool:
    """
    Invia una singola email tramite Brevo.
    Restituisce True se l'invio ha successo, False altrimenti.
    Non solleva mai eccezioni: gli errori vengono solo loggati.
    """
    if not BREVO_API_KEY:
        logger.warning("Email NON inviata (BREVO_API_KEY non impostata): %s → %s", subject, to_email)
        return False
    if not EMAIL_FROM:
        logger.warning("Email NON inviata (EMAIL_FROM non impostata): %s → %s", subject, to_email)
        return False

    payload = {
        "sender":     {"name": EMAIL_FROM_NAME, "email": EMAIL_FROM},
        "to":         [{"email": to_email, "name": to_name}],
        "subject":    subject,
        "htmlContent": html_body,
    }
    try:
        r = httpx.post(
            BREVO_SEND_URL,
            json=payload,
            headers={
                "api-key":      BREVO_API_KEY,
                "Content-Type": "application/json",
                "Accept":       "application/json",
            },
            timeout=10,
        )
        if r.status_code in (200, 201):
            logger.info("Email inviata: %s → %s", subject, to_email)
            return True
        else:
            logger.error("Errore Brevo %s: %s", r.status_code, r.text[:300])
            return False
    except Exception as exc:
        logger.error("Eccezione invio email: %s", exc)
        return False


# ══════════════════════════════════════════════════════════════════════════════
# TEMPLATE BASE (wrapper grafico comune a tutti i tipi di email)
# ══════════════════════════════════════════════════════════════════════════════

def _base_template(header_title: str, header_icon: str, accent_color: str, body_html: str) -> str:
    """
    Wrapper grafico identico all'anteprima_email.html:
    sfondo crema, card bianca con angoli arrotondati, header marrone,
    striscia colorata sotto l'header, footer grigio.

    accent_color: colore della striscia decorativa (es. #C49A3C per normale, #A63228 per ritardo)
    """
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="background:#F5F0E8;margin:0;padding:20px;font-family:'Helvetica Neue',Arial,sans-serif;">
  <div style="max-width:540px;margin:0 auto;background:#F5F0E8;padding:24px 12px;">
    <div style="background:#ffffff;border-radius:16px;overflow:hidden;
                box-shadow:0 4px 24px rgba(92,61,46,0.12);">

      <!-- HEADER -->
      <div style="background:#5C3D2E;padding:22px 28px;text-align:center;">
        <div style="font-size:13px;letter-spacing:1px;color:#E8C46A;text-transform:uppercase;
                    font-weight:600;margin-bottom:4px;">📚 Biblioteca Scolastica</div>
        <div style="font-size:22px;font-weight:700;color:#ffffff;">{header_icon} {header_title}</div>
      </div>

      <!-- STRISCIA COLORATA -->
      <div style="height:4px;background:{accent_color};"></div>

      <!-- CORPO -->
      <div style="padding:28px;">
        {body_html}
      </div>

      <!-- FOOTER -->
      <div style="background:#F5F0E8;padding:14px 28px;text-align:center;">
        <p style="margin:0;font-size:11px;color:#8B6550;">
          Comunicazione automatica della biblioteca scolastica — non rispondere a questa email.
        </p>
      </div>

    </div>
  </div>
</body>
</html>"""


def _info_table(*rows: tuple) -> str:
    """
    Genera la tabella dati (etichetta | valore) comune a tutti i template.
    Ogni `row` è una tupla (icon_label, valore).
    """
    trs = ""
    for icon_label, valore in rows:
        trs += f"""<tr>
          <td style="padding:9px 0;color:#8B6550;font-size:13px;font-weight:600;
                     text-transform:uppercase;letter-spacing:.4px;width:42%;">{icon_label}</td>
          <td style="padding:9px 0;color:#1A1208;font-size:15px;font-weight:600;">{valore}</td>
        </tr>"""
    return f"""<table style="width:100%;border-collapse:collapse;
                              border-top:1px solid #EDE5D4;
                              border-bottom:1px solid #EDE5D4;margin:18px 0;">
      {trs}
    </table>"""


# ══════════════════════════════════════════════════════════════════════════════
# 1. CONFERMA PRESTITO (allo studente, subito dopo il prestito)
# ══════════════════════════════════════════════════════════════════════════════

def build_loan_confirm_html(
    student_name: str,
    book_title: str,
    due_date_str: str,          # es. "05/07/2026"
) -> str:
    body = f"""
      <p style="font-size:16px;color:#1A1208;margin:0 0 14px;">
        Ciao <strong>{student_name}</strong>! 👋
      </p>
      <p style="font-size:15px;color:#1A1208;margin:0;">
        Hai preso in prestito questo libro dalla biblioteca:
      </p>
      {_info_table(
          ("📖 Libro",              book_title),
          ("📅 Da restituire entro", due_date_str),
      )}
      <p style="font-size:14px;color:#8B6550;margin:0;">
        Ti scriveremo un promemoria il giorno prima della scadenza. Buona lettura! 🌟
      </p>"""
    return _base_template("Prestito confermato", "✅", "#C49A3C", body)


def send_loan_confirm(student_name: str, student_email: str,
                      book_title: str, due_date_str: str) -> bool:
    html = build_loan_confirm_html(student_name, book_title, due_date_str)
    return send_email(
        to_email=student_email,
        to_name=student_name,
        subject=f"📖 Prestito confermato: {book_title}",
        html_body=html,
    )


# ══════════════════════════════════════════════════════════════════════════════
# 2. REMINDER PRE-SCADENZA (allo studente, 1 giorno prima)
# ══════════════════════════════════════════════════════════════════════════════

def build_due_soon_html(
    student_name: str,
    book_title: str,
    due_date_str: str,
) -> str:
    body = f"""
      <p style="font-size:16px;color:#1A1208;margin:0 0 14px;">
        Ciao <strong>{student_name}</strong>! 👋
      </p>
      <p style="font-size:15px;color:#1A1208;margin:0;">
        Promemoria: domani scade il prestito di questo libro 👇
      </p>
      {_info_table(
          ("📖 Libro",   book_title),
          ("⏰ Scade il", due_date_str),
      )}
      <p style="font-size:14px;color:#8B6550;margin:0;">
        Ricordati di riportarlo in biblioteca entro domani. Grazie! 📚
      </p>"""
    return _base_template("Scade domani!", "⏰", "#C49A3C", body)


def send_due_soon(student_name: str, student_email: str,
                  book_title: str, due_date_str: str) -> bool:
    html = build_due_soon_html(student_name, book_title, due_date_str)
    return send_email(
        to_email=student_email,
        to_name=student_name,
        subject=f"⏰ Scade domani: {book_title}",
        html_body=html,
    )


# ══════════════════════════════════════════════════════════════════════════════
# 3. PROMEMORIA RITARDO (allo studente, ogni giorno dopo la scadenza)
# ══════════════════════════════════════════════════════════════════════════════

def build_overdue_student_html(
    student_name: str,
    book_title: str,
    due_date_str: str,
    days_late: int,
) -> str:
    giorni = "giorno" if days_late == 1 else "giorni"
    body = f"""
      <p style="font-size:16px;color:#1A1208;margin:0 0 14px;">
        Ciao <strong>{student_name}</strong>! 👋
      </p>
      <p style="font-size:15px;color:#1A1208;margin:0;">
        Questo libro è in ritardo di
        <strong style="color:#A63228;">{days_late} {giorni}</strong>:
      </p>
      {_info_table(
          ("📖 Libro",    book_title),
          ("📅 Scadenza", due_date_str),
          ("⏳ Ritardo",  f"{days_late} {giorni}"),
      )}
      <p style="font-size:14px;color:#8B6550;margin:0;">
        Riportalo in biblioteca appena possibile, grazie per la collaborazione! 🙏
      </p>"""
    return _base_template("Libro in ritardo", "📕", "#A63228", body)


def send_overdue_student(student_name: str, student_email: str,
                         book_title: str, due_date_str: str, days_late: int) -> bool:
    html = build_overdue_student_html(student_name, book_title, due_date_str, days_late)
    giorni = "giorno" if days_late == 1 else "giorni"
    return send_email(
        to_email=student_email,
        to_name=student_name,
        subject=f"📕 Libro in ritardo da {days_late} {giorni}: {book_title}",
        html_body=html,
    )


# ══════════════════════════════════════════════════════════════════════════════
# 4. ALERT AL PROFESSORE (2 e 5 giorni dopo la scadenza)
# ══════════════════════════════════════════════════════════════════════════════

def build_overdue_teacher_html(
    teacher_name: str,
    student_name: str,
    class_name: str,
    book_title: str,
    due_date_str: str,
    days_late: int,
    is_followup: bool = False,
) -> str:
    giorni = "giorno" if days_late == 1 else "giorni"
    intro = (
        "Le segnalo che un alunno della sua classe è <strong>ancora in ritardo</strong> "
        "nella restituzione di un libro."
        if is_followup else
        "Le segnalo che un alunno della sua classe è in ritardo nella restituzione di un libro."
    )
    body = f"""
      <p style="font-size:15px;color:#1A1208;margin:0 0 14px;">
        Gentile <strong>{teacher_name}</strong>,
      </p>
      <p style="font-size:15px;color:#1A1208;margin:0;">{intro}</p>
      {_info_table(
          ("🧑‍🎓 Alunno/a", student_name),
          ("🏫 Classe",    class_name),
          ("📖 Libro",     book_title),
          ("📅 Scadenza",  due_date_str),
          ("⏳ Ritardo",   f"{days_late} {giorni}"),
      )}
      <p style="font-size:14px;color:#8B6550;margin:0;">
        Potrebbe gentilmente ricordarlo all'alunno/a? Grazie per la collaborazione. 🙏
      </p>"""
    title = "Ancora in ritardo" if is_followup else "Alunno in ritardo"
    icon  = "🔴" if is_followup else "⚠️"
    return _base_template(title, icon, "#A63228", body)


def send_overdue_teacher(teacher_name: str, teacher_email: str,
                         student_name: str, class_name: str,
                         book_title: str, due_date_str: str,
                         days_late: int, is_followup: bool = False) -> bool:
    html = build_overdue_teacher_html(
        teacher_name, student_name, class_name,
        book_title, due_date_str, days_late, is_followup,
    )
    giorni = "giorno" if days_late == 1 else "giorni"
    prefix = "🔴 Ancora in ritardo" if is_followup else "⚠️ Alunno in ritardo"
    return send_email(
        to_email=teacher_email,
        to_name=teacher_name,
        subject=f"{prefix}: {student_name} — {book_title} ({days_late} {giorni})",
        html_body=html,
    )


# ══════════════════════════════════════════════════════════════════════════════
# 5. PROMEMORIA MANUALE (admin o professore → studente specifico o tutti)
# ══════════════════════════════════════════════════════════════════════════════

def send_manual_reminder(student_name: str, student_email: str,
                         book_title: str, due_date_str: str,
                         days_late: int) -> bool:
    """Uguale al promemoria automatico di ritardo, ma inviato su richiesta manuale."""
    return send_overdue_student(student_name, student_email, book_title, due_date_str, days_late)


# ══════════════════════════════════════════════════════════════════════════════
# 5. RESET PASSWORD DOCENTE
# ══════════════════════════════════════════════════════════════════════════════

def build_reset_password_html(teacher_name: str, reset_url: str) -> str:
    body = f"""
      <p style="font-size:16px;color:#1A1208;margin:0 0 14px;">
        Gentile <strong>{teacher_name}</strong>,
      </p>
      <p style="font-size:15px;color:#1A1208;margin:0 0 18px;">
        Hai richiesto il reset della tua password per il portale docenti.
        Clicca il pulsante qui sotto per scegliere una nuova password.
      </p>
      <div style="text-align:center;margin:24px 0;">
        <a href="{reset_url}"
           style="background:#5C3D2E;color:#E8C46A;padding:14px 32px;border-radius:10px;
                  font-weight:700;font-size:15px;text-decoration:none;display:inline-block;">
          🔑 Reimposta password
        </a>
      </div>
      <p style="font-size:13px;color:#8B6550;margin:0;">
        Il link è valido per <strong>1 ora</strong>. Se non hai richiesto il reset,
        ignora questa email — la tua password rimane invariata.
      </p>"""
    return _base_template("Reimposta password", "🔑", "#C49A3C", body)


# ══════════════════════════════════════════════════════════════════════════════
# 6. BADGE SBLOCCATO (allo studente, quando ottiene un nuovo traguardo)
# ══════════════════════════════════════════════════════════════════════════════

def build_badge_earned_html(student_name: str, badge_icon: str, badge_title: str,
                             badge_description: str) -> str:
    body = f"""
      <p style="font-size:16px;color:#1A1208;margin:0 0 14px;">
        Complimenti <strong>{student_name}</strong>! 🎉
      </p>
      <p style="font-size:15px;color:#1A1208;margin:0 0 18px;">
        Hai sbloccato un nuovo traguardo nella sfida "Topo da Biblioteca":
      </p>
      <div style="text-align:center;margin:20px 0;padding:22px;background:#FBF6EC;
                  border-radius:14px;border:2px solid #E8C46A;">
        <div style="font-size:46px;line-height:1;margin-bottom:8px;">{badge_icon}</div>
        <div style="font-size:19px;font-weight:700;color:#5C3D2E;margin-bottom:6px;">{badge_title}</div>
        <div style="font-size:13.5px;color:#8B6550;">{badge_description}</div>
      </div>
      <p style="font-size:14px;color:#8B6550;margin:0;">
        Continua così: puoi vedere tutti i tuoi traguardi nel tuo profilo. Buona lettura! 📚
      </p>"""
    return _base_template("Nuovo traguardo!", "🏆", "#E8C46A", body)


def send_badge_earned(student_name: str, student_email: str, badge_icon: str,
                       badge_title: str, badge_description: str) -> bool:
    if not student_email:
        return False
    html = build_badge_earned_html(student_name, badge_icon, badge_title, badge_description)
    return send_email(
        to_email=student_email,
        to_name=student_name,
        subject=f"🏆 Nuovo traguardo sbloccato: {badge_title}",
        html_body=html,
    )


# ══════════════════════════════════════════════════════════════════════════════
# 7. MESSAGGIO DALL'AMMINISTRATORE (broadcast a studenti/professori)
# ══════════════════════════════════════════════════════════════════════════════

def build_broadcast_html(recipient_name: str, subject: str, message: str) -> str:
    # Il messaggio è testo libero scritto dall'admin: preserviamo solo i
    # ritorni a capo (nessun altro HTML, per evitare injection nel template).
    import html as _html
    safe_message = _html.escape(message).replace("\n", "<br>")
    body = f"""
      <p style="font-size:16px;color:#1A1208;margin:0 0 14px;">
        Ciao <strong>{recipient_name}</strong>,
      </p>
      <p style="font-size:15px;color:#1A1208;margin:0 0 6px;">
        Hai ricevuto una comunicazione dalla biblioteca scolastica:
      </p>
      <div style="margin:18px 0;padding:18px 20px;background:#FBF6EC;
                  border-left:4px solid #C49A3C;border-radius:8px;
                  font-size:15px;color:#1A1208;line-height:1.6;">
        {safe_message}
      </div>"""
    return _base_template(subject or "Comunicazione", "📢", "#5C3D2E", body)


def send_broadcast(recipient_name: str, recipient_email: str, subject: str, message: str) -> bool:
    if not recipient_email:
        return False
    html = build_broadcast_html(recipient_name, subject, message)
    return send_email(
        to_email=recipient_email,
        to_name=recipient_name,
        subject=f"📢 {subject}" if subject else "📢 Comunicazione dalla biblioteca",
        html_body=html,
    )
