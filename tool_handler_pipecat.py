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
    r"\bservus\b",
)


def reset_call_state() -> None:
    global appointment_done, partner_farewell_detected
    appointment_done = False
    partner_farewell_detected = False
    call_ended.clear()


def mark_partner_farewell(text: str) -> bool:
    global partner_farewell_detected

    if partner_farewell_detected:
        return True

    normalized = (text or "").lower()
    for pattern in _FAREWELL_PATTERNS:
        if re.search(pattern, normalized):
            partner_farewell_detected = True
            logger.info("Partner-Verabschiedung erkannt.")
            return True
    return False


async def handle_check_availability(params: FunctionCallParams):
    """Prüft verfügbare Termine über Calendly."""
    days = params.arguments.get("days_ahead", 5)
    if not calendly_service.is_configured():
        await params.result_callback({
            "available_slots": "Calendly nicht konfiguriert. Bitte frage den Partner nach einem passenden Termin."
        })
        return

    slots_text = await calendly_service.format_available_slots(days_ahead=days)
    await params.result_callback({"available_slots": slots_text})


async def handle_schedule_appointment(params: FunctionCallParams):
    """Verarbeitet den schedule_appointment Function Call.

    Pipecat ruft diese Funktion automatisch auf, wenn Gemini den Tool Call auslöst.
    Die Tool Response wird automatisch von Pipecat an Gemini zurückgesendet.
    """
    global appointment_done

    payload = params.arguments
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

    # E-Mail-Benachrichtigung an Mitarbeiter senden (als Terminvorschlag)
    email_sent = email_service.send_appointment_proposal(
        partner_name=payload.get("partner_name", "Unbekannt"),
        appointment_date=payload.get("appointment_date", ""),
        contact_method=payload.get("contact_method", ""),
        notes=payload.get("notes", ""),
        status=payload.get("status", "scheduled"),
        calendly_link=booking_url,
    )

    appointment_done = True

    result = {"status": "recorded"}
    if booking_url:
        result["calendly_booking_url"] = booking_url
    if email_sent:
        result["email_notification"] = "sent"
    await params.result_callback(result)


async def handle_end_call(params: FunctionCallParams):
    """Beendet den Anruf aktiv."""
    global appointment_done

    reason = params.arguments.get("reason", "completed")

    if not partner_farewell_detected:
        await params.result_callback({
            "status": "deferred",
            "reason": "waiting_for_partner_farewell",
            "message": "Partner hat sich noch nicht verabschiedet. Bitte freundlich abschließen und auf Verabschiedung warten.",
        })
        return

    logger.info(f"Anruf wird beendet. Grund: {reason}")

    appointment_done = True
    await params.result_callback({"status": "call_ended", "reason": reason})
    call_ended.set()
