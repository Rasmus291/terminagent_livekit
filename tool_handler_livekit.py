import asyncio
import logging
import re

import calendly_service
import email_service

logger = logging.getLogger(__name__)

crm_data_saved: dict = {}
partner_farewell_detected = False
assistant_farewell_detected = False
call_ended = asyncio.Event()
pending_end_call = False
_ALLOWED_CONTACT_METHODS = {"phone"}
_CONTACT_METHOD_ALIASES = {
    "telefon": "phone",
    "telefonisch": "phone",
    "phone": "phone",
    "anruf": "phone",
}

_FAREWELL_PATTERNS = (
    r"\btsch(u|ü)ss\b",
    r"\bauf wiederh(ö|oe)ren\b",
    r"\bauf wiedersehen\b",
    r"\bbis dann\b",
    r"\bbis bald\b",
    r"\bbis sp(ä|ae)ter\b",
    r"\bbis zum termin\b",
    r"\bvielen dank\b",
    r"\bsch(ö|oe)nen tag noch\b",
    r"\beinen sch(ö|oe)nen tag\b",
    r"\balles gute\b",
    r"\bmach'?s gut\b",
    r"\bciao\b",
    r"\bwiedersehen\b",
    r"\bade\b",
    r"\badiö\b",
    r"\bpfiat di\b",
    r"\bahoi\b",
)


def _is_strict_farewell(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False

    if "?" in normalized:
        return False

    tokens = re.findall(r"\w+", normalized)
    if len(tokens) > 25:
        return False

    for pattern in _FAREWELL_PATTERNS:
        if re.search(pattern, normalized):
            return True
    return False


def has_confirmed_appointment() -> bool:
    return (
        (crm_data_saved.get("status") or "").strip().lower() == "scheduled"
        and bool((crm_data_saved.get("appointment_date") or "").strip())
    )


def reset_call_state() -> None:
    global partner_farewell_detected, assistant_farewell_detected, pending_end_call
    partner_farewell_detected = False
    assistant_farewell_detected = False
    pending_end_call = False
    crm_data_saved.clear()
    call_ended.clear()


def _trigger_end_if_both_farewells(source: str) -> bool:
    if partner_farewell_detected and assistant_farewell_detected and not call_ended.is_set():
        logger.info("Beidseitige Verabschiedung erkannt (%s). Beende Gespräch automatisch.", source)
        call_ended.set()
        return True
    return False


def mark_partner_farewell(text: str) -> bool:
    global partner_farewell_detected

    if partner_farewell_detected:
        return True

    if _is_strict_farewell(text):
        partner_farewell_detected = True
        logger.info("Partner-Verabschiedung erkannt.")
        if pending_end_call and not call_ended.is_set():
            logger.info("Partner-Verabschiedung nach früherem end_call erkannt. Beende Gespräch jetzt.")
            call_ended.set()
        _trigger_end_if_both_farewells("partner")
        return True
    return False


def mark_assistant_farewell(text: str) -> bool:
    global assistant_farewell_detected

    if assistant_farewell_detected:
        return True

    if _is_strict_farewell(text):
        assistant_farewell_detected = True
        logger.info("Agent-Verabschiedung erkannt.")
        _trigger_end_if_both_farewells("assistant")
        return True
    return False


async def check_availability(days_ahead: int = 5) -> dict:
    days = max(1, min(int(days_ahead or 5), 7))

    if not calendly_service.is_configured():
        return {
            "available_slots": "Calendly nicht konfiguriert. Bitte frage den Partner nach einem passenden Termin.",
        }

    slots_text = await calendly_service.format_available_slots(days_ahead=days)
    return {"available_slots": slots_text}


async def schedule_appointment(
    partner_name: str,
    status: str,
    appointment_date: str = "",
    contact_method: str = "",
    notes: str = "",
) -> dict:
    normalized_status = (status or "").strip().lower()
    normalized_contact_method_raw = (contact_method or "").strip().lower()
    normalized_contact_method = _CONTACT_METHOD_ALIASES.get(
        normalized_contact_method_raw,
        normalized_contact_method_raw,
    )

    if normalized_status == "scheduled" and has_confirmed_appointment():
        existing_partner_name = crm_data_saved.get("partner_name", "")
        existing_appointment_date = crm_data_saved.get("appointment_date", "")
        existing_contact_method = crm_data_saved.get("contact_method", "")

        logger.info(
            "Termin bereits bestätigt. Ignoriere erneuten schedule_appointment-Aufruf."
        )
        return {
            "status": "already_scheduled",
            "partner_name": existing_partner_name,
            "appointment_date": existing_appointment_date,
            "contact_method": existing_contact_method,
            "message": "Ein Termin steht bereits fest. Frage nicht noch einmal nach einem neuen Termin. Bestätige den bereits vereinbarten Termin kurz und verabschiede dich freundlich.",
        }

    if normalized_status == "scheduled":
        missing_fields: list[str] = []
        if not (appointment_date or "").strip():
            missing_fields.append("appointment_date")
        if missing_fields:
            logger.warning("Termin noch unvollständig, fehlende Felder: %s", ", ".join(missing_fields))
            return {
                "status": "needs_more_info",
                "missing_fields": missing_fields,
                "message": "Bitte frage noch nach fehlenden Angaben (konkrete Terminzeit), bevor du den Termin speicherst.",
            }
        if not normalized_contact_method:
            normalized_contact_method = "phone"
        if normalized_contact_method not in _ALLOWED_CONTACT_METHODS:
            logger.warning("Ungültige Kontaktart für Termin: %s", contact_method)
            return {
                "status": "needs_more_info",
                "missing_fields": ["contact_method"],
                "message": "Erlaubte Kontaktart ist nur Telefon. Speichere den Termin bitte telefonisch.",
            }

    payload = {
        "partner_name": partner_name,
        "status": normalized_status or status,
        "appointment_date": appointment_date,
        "contact_method": normalized_contact_method or contact_method,
        "notes": notes,
    }
    crm_data_saved.update(payload)

    logger.info("Terminvereinbarung empfangen:")
    logger.info("  Status: %s", status)
    logger.info("  Partner: %s", partner_name)
    if appointment_date:
        logger.info("  Termin: %s", appointment_date)
    if contact_method:
        logger.info("  Kontaktart: %s", contact_method)
    if notes:
        logger.info("  Notizen: %s", notes)

    effective_status = normalized_status or status

    booking_url = None
    if calendly_service.is_configured() and effective_status == "scheduled" and appointment_date:
        try:
            booking_url = await calendly_service.create_scheduling_link(appointment_date=appointment_date)
            logger.info("  Calendly Buchungslink: %s", booking_url)
        except Exception as e:
            logger.warning("  Calendly Buchungslink konnte nicht erstellt werden: %s", e)

    if booking_url:
        crm_data_saved["calendly_link"] = booking_url

    # E-Mail wird nach Gesprächsende in finalize_session gesendet,
    # damit die Gesprächsanalyse in der Mail enthalten ist.

    result = {"status": "recorded"}
    if booking_url:
        result["calendly_booking_url"] = booking_url
    return result


async def end_call(reason: str = "completed") -> dict:
    """
    Beendet den Anruf sofort und zuverlässig.
    """
    global pending_end_call
    logger.info(f"end_call() aufgerufen. reason={reason}")
    
    logger.info("=" * 60)
    logger.info("🛑 ANRUF WIRD BEENDET")
    logger.info(f"Grund: {reason}")
    logger.info(f"Partner-Name: {crm_data_saved.get('partner_name', 'Unbekannt')}")
    logger.info(f"Termin: {crm_data_saved.get('appointment_date', '-')}")
    logger.info(f"Status: {crm_data_saved.get('status', '-')}")
    logger.info("=" * 60)
    
    pending_end_call = False
    call_ended.set()
    
    return {
        "status": "call_ended",
        "reason": reason,
        "partner": crm_data_saved.get("partner_name", ""),
        "appointment": crm_data_saved.get("appointment_date", ""),
    }
