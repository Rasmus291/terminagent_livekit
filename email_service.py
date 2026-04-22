"""
E-Mail-Benachrichtigung für Terminvorschläge.

Sendet eine E-Mail an den Mitarbeiter mit den Termindetails,
wenn der Agent einen Termin vereinbart hat.
Enthält einen Calendly-Link zur Bestätigung.

Benötigt in .env:
  SMTP_HOST=smtp.gmail.com  (oder anderer SMTP-Server)
  SMTP_PORT=587
  SMTP_USER=deine@email.de
  SMTP_PASSWORD=app_passwort
  NOTIFICATION_EMAIL=mitarbeiter@firma.de
  SENDER_NAME=LaVita Terminagent
"""

import os
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
NOTIFICATION_EMAIL = os.getenv("NOTIFICATION_EMAIL", "karian.scheer@lavita.de, rasmus.sonst@lavita.de")
SENDER_NAME = os.getenv("SENDER_NAME", "LaVita Terminagent")


def is_configured() -> bool:
    # Prüfe dass echte Konfigurationswerte (nicht Platzhalter) vorhanden sind
    if not SMTP_USER or not SMTP_PASSWORD:
        return False
    # Ausschlie Platzhalter
    if SMTP_USER == "deine@firma.de" or SMTP_PASSWORD == "dein_passwort_oder_app_kennwort":
        logger.warning("E-Mail-Konfiguration enthält Platzhalter. Bitte .env mit echten Werten aktualisieren.")
        return False
    return True


def send_appointment_proposal(
    partner_name: str,
    appointment_date: str,
    contact_method: str,
    notes: str,
    status: str,
    calendly_link: str | None = None,
) -> bool:
    """
    Sendet eine E-Mail mit dem Terminvorschlag an den Mitarbeiter.

    Returns:
        True bei Erfolg, False bei Fehler.
    """
    if not is_configured():
        logger.warning("E-Mail nicht konfiguriert (SMTP_USER/SMTP_PASSWORD fehlen). Überspringe Benachrichtigung.")
        return False

    subject = f"Neuer Terminvorschlag: {partner_name}"
    if status == "declined":
        subject = f"Absage: {partner_name}"
    elif status == "callback":
        subject = f"Rückruf gewünscht: {partner_name}"

    # Kontaktart lesbar
    contact_display = {
        "phone": "Telefon",
        "video": "Video-Call",
        "in_person": "Vor Ort",
    }.get(contact_method, contact_method or "Nicht angegeben")

    # HTML E-Mail
    html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
        <h2 style="color: #2c5530;">{'📅 Neuer Terminvorschlag' if status == 'scheduled' else '📞 Rückruf gewünscht' if status == 'callback' else '❌ Absage'}</h2>

        <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
            <tr style="border-bottom: 1px solid #eee;">
                <td style="padding: 10px; font-weight: bold; width: 160px;">Partner</td>
                <td style="padding: 10px;">{partner_name}</td>
            </tr>
            <tr style="border-bottom: 1px solid #eee;">
                <td style="padding: 10px; font-weight: bold;">Status</td>
                <td style="padding: 10px;">{'✅ Termin vereinbart' if status == 'scheduled' else '🔄 Rückruf' if status == 'callback' else '❌ Abgelehnt'}</td>
            </tr>
            {'<tr style="border-bottom: 1px solid #eee;"><td style="padding: 10px; font-weight: bold;">Datum & Uhrzeit</td><td style="padding: 10px;">' + appointment_date + '</td></tr>' if appointment_date else ''}
            <tr style="border-bottom: 1px solid #eee;">
                <td style="padding: 10px; font-weight: bold;">Kontaktart</td>
                <td style="padding: 10px;">{contact_display}</td>
            </tr>
            {'<tr style="border-bottom: 1px solid #eee;"><td style="padding: 10px; font-weight: bold;">Notizen</td><td style="padding: 10px;">' + notes + '</td></tr>' if notes else ''}
        </table>

        {f'<p><a href="{calendly_link}" style="display: inline-block; background-color: #2c5530; color: white; padding: 12px 24px; text-decoration: none; border-radius: 5px; margin: 10px 0;">✅ Termin in Calendly bestätigen</a></p>' if calendly_link and status == 'scheduled' else ''}

        <p style="color: #888; font-size: 12px; margin-top: 30px;">
            Diese E-Mail wurde automatisch vom LaVita Terminagent erstellt.
        </p>
    </div>
    """

    # Nur-Text Fallback
    text = f"""Neuer Terminvorschlag

Partner: {partner_name}
Status: {status}
Datum: {appointment_date or 'Nicht festgelegt'}
Kontaktart: {contact_display}
Notizen: {notes or '-'}
{f'Calendly-Link: {calendly_link}' if calendly_link else ''}
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{SENDER_NAME} <{SMTP_USER}>"
    msg["To"] = NOTIFICATION_EMAIL
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        logger.debug(f"Verbinde zu SMTP-Server {SMTP_HOST}:{SMTP_PORT}...")
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            logger.debug("Starte TLS...")
            server.starttls()
            logger.debug(f"Authentifiziere als {SMTP_USER}...")
            server.login(SMTP_USER, SMTP_PASSWORD)
            logger.debug("Versende E-Mail...")
            server.sendmail(SMTP_USER, NOTIFICATION_EMAIL, msg.as_string())
        logger.info(f"✅ Terminvorschlag-Mail erfolgreich an {NOTIFICATION_EMAIL} gesendet")
        return True
    except smtplib.SMTPAuthenticationError as e:
        logger.error(f"❌ SMTP-Authentifizierung fehlgeschlagen (535): {e}")
        logger.error("  → Prüfe SMTP_USER und SMTP_PASSWORD in .env")
        logger.error("  → Für Office365: Verwende App-Passwort wenn 2FA aktiviert")
        logger.error("  → Mail-Bestätigung konnte nicht gesendet werden, Termin aber gespeichert.")
        return False
    except smtplib.SMTPException as e:
        logger.error(f"❌ SMTP-Fehler beim E-Mail-Versand: {e}")
        logger.error("  → Mail-Bestätigung konnte nicht gesendet werden, Termin aber gespeichert.")
        return False
    except Exception as e:
        logger.error(f"❌ Unerwarteter Fehler beim E-Mail-Versand: {e}")
        logger.error("  → Mail-Bestätigung konnte nicht gesendet werden, Termin aber gespeichert.")
        return False
