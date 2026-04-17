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

    logger.info("Termin verarbeitet. Gespräch wird beendet...")

    # Warte bis ausstehende Audio-Chunks abgespielt sind
    while not audio_streamer.output_queue.empty():
        await asyncio.sleep(0.5)
    await asyncio.sleep(2.0)

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
            await handle_schedule_appointment(fc, session, crm_data_saved, audio_streamer)
            return True  # Signal: Gespräch beenden
    
    return False
