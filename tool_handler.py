import asyncio
import logging

from google.genai import types

logger = logging.getLogger(__name__)


async def handle_schedule_appointment(fc, session, crm_data_saved, audio_streamer):
    """Verarbeitet den schedule_appointment Function Call vom Modell.
    
    Returns:
        dict: Die extrahierten Termindaten (payload).
    """
    logger.info("Terminvereinbarung empfangen...")
    payload = dict(fc.args) if fc.args else {}
    crm_data_saved.update(payload)

    logger.info(f"Termin-Status: {payload.get('status', 'unbekannt')}")
    logger.info(f"Partner: {payload.get('partner_name', 'unbekannt')}")
    if payload.get('appointment_date'):
        logger.info(f"Termin: {payload.get('appointment_date')}")

    # Tool Response an Modell zurücksenden
    logger.info("Sende Tool Response zurück...")
    try:
        await session.send_tool_response(
            function_responses=[
                types.FunctionResponse(
                    id=fc.id,
                    name=fc.name,
                    response={"status": "recorded"}
                )
            ]
        )
    except Exception as send_err:
        logger.error(f"Konnte Tool Response nicht an Modell zurücksenden: {send_err}")

    logger.info("Termin verarbeitet. Gespräch bleibt aktiv für Rückfragen.")

    return payload


async def process_tool_calls(response, session, crm_data_saved, audio_streamer):
    """Verarbeitet alle Tool Calls aus einer Gemini Response.
    
    Returns:
        bool: True wenn das Gespräch beendet werden soll.
    """
    if not getattr(response, 'tool_call', None):
        return False
    
    for fc in response.tool_call.function_calls:
        logger.info(f"Function Call empfangen: {fc.name}")
        logger.info(f"Argumente: {fc.args}")
        
        if fc.name == "schedule_appointment":
            payload = await handle_schedule_appointment(fc, session, crm_data_saved, audio_streamer)
            status = str(payload.get("status", "")).strip().lower()
            if status == "scheduled":
                return True  # Nur bei bestätigtem Termin den "bald beenden"-Pfad aktivieren
            logger.info("Status=%s -> Gespräch bleibt aktiv für höflichen Abschluss.", status or "unbekannt")
            return False
    
    return False
