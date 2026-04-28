import asyncio
import logging
import re

from pipecat.services.llm_service import FunctionCallParams

import calendly_service
import email_service

logger = logging.getLogger(__name__)

# Gemeinsamer State für CRM-Daten (wird von main_pipecat.py referenziert)
crm_data_saved = {}
appointment_done = False
call_ended = asyncio.Event()
partner_farewell_detected = False
assistant_farewell_detected = False

_ALLOWED_STATUSES = {"scheduled", "declined", "callback"}
_ALLOWED_CONTACT_METHODS = {"phone"}
_CONTACT_METHOD_ALIASES = {
    "telefon": "phone",
    "telefonisch": "phone",
    "anruf": "phone",
    "phone": "phone",
}

_FAREWELL_PATTERNS = (
    r"\btsch(u|ü)ss\b",
    r"\bauf wiedersehen\b",
    r"\bauf wiederh(ö|oe)ren\b",
    r"\bbis dann\b",
    r"\bbis bald\b",
    r"\bbis sp(ä|ae)ter\b",
    r"\bbis zum termin\b",
    r"\bsch(ö|oe)nen tag noch\b",
    r"\beinen sch(ö|oe)nen tag\b",
    r"\balles gute\b",
    r"\bmach'?s gut\b",
    r"\bciao\b",
)


def _is_strict_farewell(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False

    # Keine Verabschiedung während Terminfindung/Rückfrage interpretieren
    if "?" in normalized:
        return False

    tokens = re.findall(r"\w+", normalized)
    if len(tokens) > 10:
        return False

    for pattern in _FAREWELL_PATTERNS:
        if re.search(pattern, normalized):
            return True
    return False


def reset_call_state() -> None:
    global appointment_done, partner_farewell_detected, assistant_farewell_detected
    appointment_done = False
    partner_farewell_detected = False
    assistant_farewell_detected = False
    call_ended.clear()


def mark_partner_farewell(text: str) -> bool:
    global partner_farewell_detected

    if partner_farewell_detected:
        return True

    if _is_strict_farewell(text):
        partner_farewell_detected = True
        logger.info("Partner-Verabschiedung erkannt.")
        return True
    return False


def mark_assistant_farewell(text: str) -> bool:
    global assistant_farewell_detected

    if assistant_farewell_detected:
        return True

    if _is_strict_farewell(text):
        assistant_farewell_detected = True
        logger.info("Agent-Verabschiedung erkannt.")
        return True
    return False


async def handle_check_availability(params: FunctionCallParams):
    """Prüft verfügbare Termine über Calendly."""
    days = params.arguments.get("days_ahead", 5)
    if not calendly_service.is_configured():
        await params.result_callback({
            "available_slots": "Calendly nicht konfiguriert. Bitte frage den Partner nach einem passenden Termin.",
            "message": "Ich habe gerade keinen Live-Kalenderzugriff. Frage bitte nach einem konkreten Wunschtermin und bestätige, dass wir ihn intern einplanen.",
        })
        return

    try:
        slots_text = await calendly_service.format_available_slots(days_ahead=days)
        await params.result_callback({"available_slots": slots_text})
    except Exception as e:
        logger.warning("check_availability fehlgeschlagen: %s", e)
        await params.result_callback({
            "available_slots": "Kalender aktuell nicht erreichbar.",
            "message": "Bitte entschuldige kurz die technische Störung und frage direkt nach einem Wunschtermin des Partners.",
        })


async def handle_schedule_appointment(params: FunctionCallParams):
    """Verarbeitet den schedule_appointment Function Call.

    Pipecat ruft diese Funktion automatisch auf, wenn Gemini den Tool Call auslöst.
    Die Tool Response wird automatisch von Pipecat an Gemini zurückgesendet.
    """
    global appointment_done

    payload = params.arguments
    normalized_status = str(payload.get("status", "")).strip().lower()
    normalized_contact_method_raw = str(payload.get("contact_method", "")).strip().lower()
    normalized_contact_method = _CONTACT_METHOD_ALIASES.get(
        normalized_contact_method_raw,
        normalized_contact_method_raw,
    )

    if normalized_status and normalized_status not in _ALLOWED_STATUSES:
        await params.result_callback({
            "status": "needs_more_info",
            "message": "Bitte nutze nur die Statuswerte scheduled, declined oder callback und bestätige den Sachverhalt kurz mit dem Partner.",
        })
        return

    if normalized_status == "scheduled":
        missing_fields = []
        if not str(payload.get("appointment_date", "")).strip():
            missing_fields.append("appointment_date")
        if not normalized_contact_method:
            normalized_contact_method = "phone"
        elif normalized_contact_method not in _ALLOWED_CONTACT_METHODS:
            missing_fields.append("contact_method")

        if missing_fields:
            await params.result_callback({
                "status": "needs_more_info",
                "missing_fields": missing_fields,
                "message": "Bitte frage noch kurz nach fehlenden Angaben: konkrete Terminzeit.",
            })
            return

    if normalized_contact_method:
        payload["contact_method"] = normalized_contact_method
    if normalized_status:
        payload["status"] = normalized_status

    crm_data_saved.update(payload)

    logger.info(f"Terminvereinbarung empfangen:")
    logger.info(f"  Status: {payload.get('status', 'unbekannt')}")
    logger.info(f"  Partner: {payload.get('partner_name', 'unbekannt')}")
    if payload.get("appointment_date"):
        logger.info(f"  Termin: {payload.get('appointment_date')}")
    if payload.get("contact_method"):
        logger.info(f"  Kontaktart: {payload.get('contact_method')}")
    if payload.get("notes"):
        logger.info(f"  Notizen: {payload.get('notes')}")

    # Calendly-Buchungslink erstellen, falls konfiguriert und Termin vereinbart
    booking_url = None
    if calendly_service.is_configured() and payload.get("status") == "scheduled":
        try:
            booking_url = await calendly_service.create_scheduling_link(
                appointment_date=payload.get("appointment_date"),
            )
            logger.info(f"  Calendly Buchungslink: {booking_url}")
        except Exception as e:
            logger.warning(f"  Calendly Buchungslink konnte nicht erstellt werden: {e}")

    if booking_url:
        crm_data_saved["calendly_link"] = booking_url

    # E-Mail wird nach Gesprächsende in finalize_session gesendet,
    # damit die Gesprächsanalyse in der Mail enthalten ist.

    appointment_done = True

    result = {"status": "recorded"}
    if booking_url:
        result["calendly_booking_url"] = booking_url
    await params.result_callback(result)


async def handle_end_call(params: FunctionCallParams):
    """Beendet den Anruf aktiv."""
    global appointment_done

    reason = params.arguments.get("reason", "completed")

    if not assistant_farewell_detected:
        await params.result_callback({
            "status": "deferred",
            "reason": "waiting_for_assistant_farewell",
            "message": "Bitte verabschiede dich zuerst genau einmal klar und freundlich, danach end_call erneut auslösen.",
        })
        return

    if not partner_farewell_detected:
        await params.result_callback({
            "status": "deferred",
            "reason": "waiting_for_partner_farewell",
            "message": "Partner hat sich noch nicht verabschiedet. Bitte freundlich mit einem kurzen Abschlusssatz enden und auf die Verabschiedung warten.",
        })
        return

    logger.info(f"Anruf wird beendet. Grund: {reason}")

    appointment_done = True
    await params.result_callback({"status": "call_ended", "reason": reason})
    call_ended.set()
