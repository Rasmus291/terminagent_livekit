"""
E-Mail-Benachrichtigung für Terminvorschläge.

Sendet eine E-Mail an den Mitarbeiter mit den Termindetails,
wenn der Agent einen Termin vereinbart hat.
Enthält einen Microsoft Kalender-Link zum Event.

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
import re
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.parse import quote

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


def _parse_recipients(raw_recipients: str) -> list[str]:
    recipients = [entry.strip() for entry in re.split(r"[;,]", raw_recipients or "") if entry.strip()]
    return recipients


def _parse_appointment_datetime(date_str: str) -> datetime | None:
    """Parst den Termin-String in ein datetime-Objekt."""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S",
                "%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None


def _build_google_calendar_link(
    partner_name: str,
    appointment_date: str,
    _contact_method: str,
    notes: str,
) -> str | None:
    """Erstellt einen Google Calendar One-Click-Link."""
    dt = _parse_appointment_datetime(appointment_date)
    if not dt:
        return None

    start = dt.strftime("%Y%m%dT%H%M%S")
    end = (dt + timedelta(hours=1)).strftime("%Y%m%dT%H%M%S")

    title = f"Termin mit {partner_name}"
    details = ""
    if notes:
        details = f"Notizen: {notes}"

    params = (
        f"action=TEMPLATE"
        f"&text={quote(title)}"
        f"&dates={start}/{end}"
        f"&details={quote(details)}"
        f"&ctz=Europe/Berlin"
    )
    return f"https://calendar.google.com/calendar/render?{params}"


def _build_outlook_calendar_link(
    partner_name: str,
    appointment_date: str,
    _contact_method: str,
    notes: str,
) -> str | None:
    """Erstellt einen Outlook Web One-Click-Link."""
    dt = _parse_appointment_datetime(appointment_date)
    if not dt:
        return None

    start = dt.strftime("%Y-%m-%dT%H:%M:%S")
    end = (dt + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S")

    title = f"Telefontermin LaVita - {partner_name}"
    body = ""
    if notes:
        body = f"Notizen: {notes}"

    params = (
        f"path=/calendar/action/compose"
        f"&rru=addevent"
        f"&subject={quote(title)}"
        f"&startdt={start}"
        f"&enddt={end}"
        f"&body={quote(body)}"
    )
    return f"https://outlook.office.com/calendar/0/deeplink/compose?{params}"


def send_appointment_proposal(
    partner_name: str,
    appointment_date: str,
    notes: str,
    status: str,
    calendly_link: str | None = None,  # Legacy-Parameter, wird ignoriert
    analysis: dict | None = None,
) -> bool:
    """
    Sendet eine E-Mail mit dem Gesprächsergebnis und Terminvorschlag an den Mitarbeiter.

    Args:
        analysis: Dict mit Feldern zusammenfassung, sentiment_partner,
                  sentiment_gesamt, stimmung_details, ergebnis.

    Returns:
        True bei Erfolg, False bei Fehler.
    """
    if not is_configured():
        logger.warning("E-Mail nicht konfiguriert (SMTP_USER/SMTP_PASSWORD fehlen). Überspringe Benachrichtigung.")
        return False

    recipients = _parse_recipients(NOTIFICATION_EMAIL)
    if not recipients:
        logger.warning("E-Mail nicht konfiguriert (NOTIFICATION_EMAIL fehlt/ungültig). Überspringe Benachrichtigung.")
        return False

    analysis = analysis or {}

    subject = f"Neuer Terminvorschlag: {partner_name}"
    if status == "declined":
        subject = f"Absage: {partner_name}"
    elif status == "callback":
        subject = f"Rückruf gewünscht: {partner_name}"

    zusammenfassung = analysis.get("zusammenfassung", "")
    sentiment_gesamt = analysis.get("sentiment_gesamt", "unbekannt")
    sentiment_partner = analysis.get("sentiment_partner")
    stimmung_details = analysis.get("stimmung_details", "")

    sentiment_display = sentiment_gesamt.capitalize()
    if sentiment_partner is not None:
        sentiment_display += f" ({sentiment_partner}/10)"

    # Kalender-Links aufbauen (nur bei vereinbartem Termin mit Datum)
    outlook_cal_link = None
    if status == "scheduled" and appointment_date:
        outlook_cal_link = _build_outlook_calendar_link(partner_name, appointment_date, "", notes)

    # Kalender-Buttons HTML
    calendar_buttons_html = ""
    if outlook_cal_link:
        button_style = "display: inline-block; color: white; padding: 12px 24px; text-decoration: none; border-radius: 5px; margin: 5px 5px 5px 0; font-size: 14px;"
        calendar_buttons_html = '<div style="margin: 20px 0;">'
        calendar_buttons_html += '<p style="font-weight: bold; margin-bottom: 10px;">Termin direkt eintragen:</p>'
        calendar_buttons_html += f'<a href="{outlook_cal_link}" style="{button_style} background-color: #0078D4;">📅 Microsoft Kalender – Termin eintragen</a> '
        calendar_buttons_html += '</div>'

    # Zusammenfassung HTML
    zusammenfassung_html = ""
    if zusammenfassung:
        zusammenfassung_html = f"""
        <div style="background-color: #f8f9fa; border-left: 4px solid #2c5530; padding: 12px 16px; margin: 20px 0;">
            <p style="font-weight: bold; margin: 0 0 6px 0;">Gesprächszusammenfassung</p>
            <p style="margin: 0;">{zusammenfassung}</p>
        </div>
        """

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
            <tr style="border-bottom: 1px solid #eee;">
                <td style="padding: 10px; font-weight: bold;">Termin</td>
                <td style="padding: 10px;">{appointment_date or 'Nicht festgelegt'}</td>
            </tr>
            {'<tr style="border-bottom: 1px solid #eee;"><td style="padding: 10px; font-weight: bold;">Notizen</td><td style="padding: 10px;">' + notes + '</td></tr>' if notes else ''}
            <tr style="border-bottom: 1px solid #eee;">
                <td style="padding: 10px; font-weight: bold;">Stimmung</td>
                <td style="padding: 10px;">{sentiment_display}</td>
            </tr>
            {'<tr style="border-bottom: 1px solid #eee;"><td style="padding: 10px; font-weight: bold;">Stimmung Details</td><td style="padding: 10px;">' + stimmung_details + '</td></tr>' if stimmung_details else ''}
        </table>

        {zusammenfassung_html}

        {calendar_buttons_html}

        <p style="color: #888; font-size: 12px; margin-top: 30px;">
            Diese E-Mail wurde automatisch vom LaVita Terminagent erstellt.
        </p>
    </div>
    """

    # Nur-Text Fallback
    text = f"""Gesprächsergebnis – {partner_name}

Partner: {partner_name}
Status: {status}
Termin: {appointment_date or 'Nicht festgelegt'}
Notizen: {notes or '-'}
Stimmung: {sentiment_display}
{f'Stimmung Details: {stimmung_details}' if stimmung_details else ''}
{f'Zusammenfassung: {zusammenfassung}' if zusammenfassung else ''}
{f'Microsoft Kalender: {outlook_cal_link}' if outlook_cal_link else ''}
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{SENDER_NAME} <{SMTP_USER}>"
    msg["To"] = ", ".join(recipients)
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
            server.sendmail(SMTP_USER, recipients, msg.as_string())
        logger.info("✅ Terminvorschlag-Mail erfolgreich an %s gesendet", ", ".join(recipients))
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


def send_call_result_summary(
    call_start_time: str,
    call_duration_seconds: float,
    crm_data: dict | None,
    analysis: dict | None,
    transcript: list[str] | None,
) -> bool:
    """Sendet eine Ergebnis-Mail mit Gesprächsausgang, Analyse und Kalender-Link."""
    if not is_configured():
        logger.warning("Ergebnis-Mail übersprungen: SMTP nicht konfiguriert.")
        return False

    recipients = _parse_recipients(NOTIFICATION_EMAIL)
    if not recipients:
        logger.warning("Ergebnis-Mail übersprungen: NOTIFICATION_EMAIL fehlt/ungültig.")
        return False

    crm_data = crm_data or {}
    analysis = analysis or {}
    transcript = transcript or []

    partner_name = (crm_data.get("partner_name") or analysis.get("partner_name") or "Unbekannt").strip()
    status_raw = (crm_data.get("status") or analysis.get("ergebnis") or "unbekannt").strip()
    # Normalisierung: Gemini gibt manchmal verschiedene Schreibweisen zurück
    status_lower = status_raw.lower()
    if status_lower in ("scheduled", "confirmed", "terminvereinbart", "termin vereinbart", "vereinbart"):
        status = "scheduled"
    elif status_lower in ("callback", "rückruf", "rueckruf", "nochmal anrufen"):
        status = "callback"
    elif status_lower in ("declined", "abgelehnt", "kein interesse", "kein termin"):
        status = "declined"
    else:
        status = status_raw
    appointment_date = (crm_data.get("appointment_date") or analysis.get("termin") or "-").strip() or "-"
    notes = (crm_data.get("notes") or "-").strip() or "-"
    sentiment_gesamt = str(analysis.get("sentiment_gesamt") or "unbekannt")
    sentiment_partner = analysis.get("sentiment_partner")
    stimmung_details = str(analysis.get("stimmung_details") or "")
    summary = str(analysis.get("zusammenfassung") or "Keine automatische Zusammenfassung verfügbar.")

    sentiment_display = sentiment_gesamt.capitalize()
    if sentiment_partner is not None:
        sentiment_display += f" ({sentiment_partner}/10)"

    subject = f"Gesprächsergebnis: {partner_name} ({status})"
    duration_display = f"{int(round(call_duration_seconds))}s"

    # Kalender-Links aufbauen (nur bei vereinbartem Termin mit Datum)
    outlook_cal_link = None
    has_appointment = appointment_date and appointment_date != "-" and status == "scheduled"
    if has_appointment:
        outlook_cal_link = _build_outlook_calendar_link(partner_name, appointment_date, "", notes)

    # Kalender-Buttons HTML
    calendar_buttons_html = ""
    if outlook_cal_link:
        button_style = "display: inline-block; color: white; padding: 12px 24px; text-decoration: none; border-radius: 5px; margin: 5px 5px 5px 0; font-size: 14px;"
        calendar_buttons_html = '<div style="margin: 20px 0;">'
        calendar_buttons_html += '<p style="font-weight: bold; margin-bottom: 10px;">Termin direkt eintragen:</p>'
        calendar_buttons_html += f'<a href="{outlook_cal_link}" style="{button_style} background-color: #0078D4;">📅 Microsoft Kalender – Termin eintragen</a> '
        calendar_buttons_html += '</div>'

    # Zusammenfassung HTML
    zusammenfassung_html = f"""
    <div style="background-color: #f8f9fa; border-left: 4px solid #2c5530; padding: 12px 16px; margin: 20px 0;">
        <p style="font-weight: bold; margin: 0 0 6px 0;">Gesprächszusammenfassung</p>
        <p style="margin: 0;">{summary}</p>
    </div>
    """

    text = (
        f"Gesprächsergebnis \u2013 {partner_name}\n\n"
        f"Startzeit: {call_start_time}\n"
        f"Dauer: {duration_display}\n"
        f"Partner: {partner_name}\n"
        f"Status: {status}\n"
        f"Termin: {appointment_date}\n"
        f"Notizen: {notes}\n"
        f"Stimmung: {sentiment_display}\n"
        f"{f'Stimmung Details: {stimmung_details}' if stimmung_details else ''}\n"
        f"Zusammenfassung: {summary}\n"
        f"{f'Microsoft Kalender: {outlook_cal_link}' if outlook_cal_link else ''}\n\n"
        f"--- Transkript ---\n"
        + ("\n".join(transcript) if transcript else "Kein Transkript vorhanden.")
        + "\n"
    )

    html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 700px; margin: 0 auto;">
        <h2 style="color: #2c5530;">📋 Gesprächsergebnis</h2>
        <table style="width: 100%; border-collapse: collapse; margin: 16px 0;">
            <tr style="border-bottom: 1px solid #eee;"><td style="padding: 8px; font-weight: bold; width: 180px;">Startzeit</td><td style="padding: 8px;">{call_start_time}</td></tr>
            <tr style="border-bottom: 1px solid #eee;"><td style="padding: 8px; font-weight: bold;">Dauer</td><td style="padding: 8px;">{duration_display}</td></tr>
            <tr style="border-bottom: 1px solid #eee;"><td style="padding: 8px; font-weight: bold;">Partner</td><td style="padding: 8px;">{partner_name}</td></tr>
            <tr style="border-bottom: 1px solid #eee;"><td style="padding: 8px; font-weight: bold;">Status</td><td style="padding: 8px;">{'\u2705 Termin vereinbart' if status == 'scheduled' else '\U0001f504 R\u00fcckruf' if status == 'callback' else '\u274c Abgelehnt' if status == 'declined' else status}</td></tr>
            <tr style="border-bottom: 1px solid #eee;"><td style="padding: 8px; font-weight: bold;">Termin</td><td style="padding: 8px;">{appointment_date}</td></tr>
            <tr style="border-bottom: 1px solid #eee;"><td style="padding: 8px; font-weight: bold;">Stimmung</td><td style="padding: 8px;">{sentiment_display}</td></tr>
            {'<tr style="border-bottom: 1px solid #eee;"><td style="padding: 8px; font-weight: bold;">Stimmung Details</td><td style="padding: 8px;">' + stimmung_details + '</td></tr>' if stimmung_details else ''}
            <tr style="border-bottom: 1px solid #eee;"><td style="padding: 8px; font-weight: bold;">Notizen</td><td style="padding: 8px;">{notes}</td></tr>
        </table>

        {zusammenfassung_html}

        {calendar_buttons_html}

        {'<div style="background-color: #f0f2f5; padding: 12px 16px; margin: 20px 0; border-radius: 6px;"><p style="font-weight: bold; margin: 0 0 8px 0;">Transkript (' + str(len(transcript)) + ' Zeilen)</p><div style="font-size: 13px; line-height: 1.6;">' + '<br>'.join(transcript) + '</div></div>' if transcript else '<p style="color: #888;">Kein Transkript vorhanden.</p>'}

        <p style="color: #777; font-size: 12px;">Diese E-Mail wurde automatisch vom LaVita Terminagent erstellt.</p>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{SENDER_NAME} <{SMTP_USER}>"
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, recipients, msg.as_string())
        logger.info("✅ Ergebnis-Mail erfolgreich an %s gesendet", ", ".join(recipients))
        return True
    except Exception as e:
        logger.error("❌ Ergebnis-Mail konnte nicht gesendet werden: %s", e)
        return False
