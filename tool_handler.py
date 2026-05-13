import asyncio
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_ALLOWED_CONTACT_METHODS = {"phone"}
_CONTACT_METHOD_ALIASES = {
    "telefon": "phone",
    "telefonisch": "phone",
    "phone": "phone",
    "anruf": "phone",
}

_FAREWELL_PATTERNS = (
    r"\btsch(u|ü|ue)ss?\b",
    r"\bauf wiederh(ö|oe)ren\b",
    r"\bauf wiedersehen\b",
    r"\bbis dann\b",
    r"\bbis bald\b",
    r"\bbis sp(ä|ae)ter\b",
    r"\bbis zum termin\b",
    r"\bsch(ö|oe)nen tag( noch)?\b",
    r"\beinen sch(ö|oe)nen tag\b",
    r"\bsch(ö|oe)nen abend( noch)?\b",
    r"\bsch(ö|oe)nes wochenende\b",
    r"\bguten tag\b",
    r"\balles gute\b",
    r"\bmach'?s gut\b",
    r"\bciao\b",
    r"\bwiedersehen\b",
    r"\bpfiat di\b",
    r"\btsch(ö|oe)\b",
    r"\bbye\b",
    r"\bservus\b",
)


@dataclass
class CallState:
    """Isolierter Zustand pro Anruf — wird in lavita_agent() frisch erstellt.

    Jeder Anruf bekommt seine eigene Instanz, sodass kein Zustand zwischen
    aufeinanderfolgenden Gesprächen im selben Worker-Prozess übrig bleibt.
    """
    crm_data: dict = field(default_factory=dict)
    partner_farewell_detected: bool = False
    assistant_farewell_detected: bool = False
    # Event wird hier per default_factory erstellt — das ist korrekt, weil
    # CallState() immer innerhalb von lavita_agent() (= im laufenden Event Loop) aufgerufen wird.
    call_ended: asyncio.Event = field(default_factory=asyncio.Event)


def _is_strict_farewell(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    if len(re.findall(r"\w+", normalized)) > 30:
        return False
    return any(re.search(p, normalized) for p in _FAREWELL_PATTERNS)


def _trigger_end_if_both_farewells(state: CallState, source: str) -> bool:
    if state.partner_farewell_detected and state.assistant_farewell_detected and not state.call_ended.is_set():
        logger.info("Beidseitige Verabschiedung erkannt (%s). Beende Gespräch automatisch.", source)
        state.call_ended.set()
        return True
    return False


def mark_partner_farewell(state: CallState, text: str) -> bool:
    if state.partner_farewell_detected:
        return True
    if _is_strict_farewell(text):
        state.partner_farewell_detected = True
        logger.info("Partner-Verabschiedung erkannt — warte auf Agent-Antwort, dann auflegen.")
        _trigger_end_if_both_farewells(state, "partner")
        return True
    return False


def mark_assistant_farewell(state: CallState, text: str) -> bool:
    if state.assistant_farewell_detected:
        return True
    if _is_strict_farewell(text):
        state.assistant_farewell_detected = True
        logger.info("Agent-Verabschiedung erkannt.")
        _trigger_end_if_both_farewells(state, "assistant")
        return True
    return False


def has_confirmed_appointment(state: CallState) -> bool:
    return (
        (state.crm_data.get("status") or "").strip().lower() == "scheduled"
        and bool((state.crm_data.get("appointment_date") or "").strip())
    )


async def schedule_appointment(
    state: CallState,
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

    if normalized_status == "scheduled" and has_confirmed_appointment(state):
        logger.info("Termin bereits bestätigt. Ignoriere erneuten schedule_appointment-Aufruf.")
        return {
            "status": "already_scheduled",
            "partner_name": state.crm_data.get("partner_name", ""),
            "appointment_date": state.crm_data.get("appointment_date", ""),
            "contact_method": state.crm_data.get("contact_method", ""),
            "message": "Ein Termin steht bereits fest. Frage nicht noch einmal nach einem neuen Termin. Bestätige den bereits vereinbarten Termin kurz und verabschiede dich freundlich.",
        }

    if normalized_status == "scheduled":
        if not (appointment_date or "").strip():
            logger.warning("Termin noch unvollständig — appointment_date fehlt.")
            return {
                "status": "needs_more_info",
                "missing_fields": ["appointment_date"],
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
    state.crm_data.update(payload)

    logger.info("Terminvereinbarung empfangen: Status=%s Partner=%s Termin=%s",
                status, partner_name, appointment_date or "-")
    return {"status": "recorded"}


async def end_call(state: CallState, reason: str = "completed") -> dict:
    """Beendet den Anruf sofort und zuverlässig."""
    logger.info("=" * 60)
    logger.info("🛑 ANRUF WIRD BEENDET — Grund: %s", reason)
    logger.info("Partner: %s | Termin: %s | Status: %s",
                state.crm_data.get("partner_name", "Unbekannt"),
                state.crm_data.get("appointment_date", "-"),
                state.crm_data.get("status", "-"))
    logger.info("=" * 60)

    state.call_ended.set()

    return {
        "status": "call_ended",
        "reason": reason,
        "partner": state.crm_data.get("partner_name", ""),
        "appointment": state.crm_data.get("appointment_date", ""),
    }
