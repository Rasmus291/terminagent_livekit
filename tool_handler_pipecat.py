import asyncio
import logging

from pipecat.services.llm_service import FunctionCallParams

logger = logging.getLogger(__name__)

# Gemeinsamer State für CRM-Daten (wird von main_pipecat.py referenziert)
crm_data_saved = {}
appointment_done = False
call_ended = asyncio.Event()


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

    appointment_done = True

    # Pipecat sendet das Ergebnis automatisch als Tool Response an Gemini
    await params.result_callback({"status": "recorded"})


async def handle_end_call(params: FunctionCallParams):
    """Beendet den Anruf aktiv."""
    global appointment_done

    reason = params.arguments.get("reason", "completed")
    logger.info(f"Anruf wird beendet. Grund: {reason}")

    appointment_done = True
    await params.result_callback({"status": "call_ended", "reason": reason})
    call_ended.set()
