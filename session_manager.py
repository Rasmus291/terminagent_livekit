"""Session-Lifecycle: Event-Handler, Finalization und Call-Control."""

import asyncio
import datetime
import logging
import os
import time

logger = logging.getLogger(__name__)

_START_TRIGGER_PREFIX = "[START_TRIGGER]"


def create_conversation_handler(session_transcript, latencies_list, started_event, farewell_imports):
    """Erstellt den on_conversation_item Event-Handler.

    Args:
        session_transcript: Mutable list für Transkript-Einträge
        latencies_list: Mutable list für Latenz-Messungen
        started_event: asyncio.Event für Agent-Start-Erkennung
        farewell_imports: Dict mit farewell-Funktionen und -Variablen
    """
    last_user_speech_end = [None]  # Mutable container für nonlocal-Ersatz

    mark_partner_farewell = farewell_imports["mark_partner_farewell"]
    mark_assistant_farewell = farewell_imports["mark_assistant_farewell"]

    def on_conversation_item(event):
        item = getattr(event, "item", None)
        if not item:
            return
        role = getattr(item, "role", None)
        text = getattr(item, "text_content", None)
        if not text:
            return

        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        now_perf = time.perf_counter()

        if role == "user":
            if text.startswith(_START_TRIGGER_PREFIX):
                return
            last_user_speech_end[0] = now_perf
            logger.info("User: %s", text)
            session_transcript.append(f"**[{ts}] User:** {text}")
            mark_partner_farewell(text)
        elif role == "assistant":
            if last_user_speech_end[0] is not None:
                latency = now_perf - last_user_speech_end[0]
                latencies_list.append(latency)
                logger.info("⏱️ Antwort-Latenz: %.2fs", latency)
                last_user_speech_end[0] = None
            started_event.set()
            logger.info("Agent: %s", text)
            session_transcript.append(f"**[{ts}] Agent:** {text}")
            mark_assistant_farewell(text)

            # Farewell-Timer
            from tool_handler import assistant_farewell_detected, call_ended
            if assistant_farewell_detected and not call_ended.is_set():
                async def _farewell_timer():
                    await asyncio.sleep(5)
                    if not call_ended.is_set():
                        logger.info("⏰ Farewell-Timer abgelaufen — erzwinge Auflegen.")
                        call_ended.set()
                asyncio.create_task(_farewell_timer())

    return on_conversation_item


async def finalize_session(
    session_transcript, crm_data, audio_recorder,
    session_start_time, session_start_perf, session_timestamp, latencies,
):
    """Generiert Analyse, speichert Report, sendet E-Mail, speichert Audio."""
    from reporting import generate_analysis, save_session_report

    call_duration = time.perf_counter() - session_start_perf
    call_start_str = session_start_time.strftime("%Y-%m-%d %H:%M:%S")

    if latencies:
        avg = sum(latencies) / len(latencies)
        logger.info("⏱️ Latenz: Ø %.2fs | Min %.2fs | Max %.2fs | %d Turns",
                     avg, min(latencies), max(latencies), len(latencies))

    try:
        analysis = await asyncio.to_thread(generate_analysis, session_transcript)
    except Exception as e:
        logger.error("Analyse fehlgeschlagen: %s", e)
        analysis = {"zusammenfassung": f"*Fehler: {e}*", "ergebnis": "unbekannt"}

    save_session_report(
        session_transcript, crm_data=crm_data, call_duration=call_duration,
        call_start_time=call_start_str, analysis=analysis, timestamp=session_timestamp,
    )

    if crm_data:
        import email_service
        email_service.send_appointment_proposal(
            partner_name=crm_data.get("partner_name", "Unbekannt"),
            appointment_date=crm_data.get("appointment_date", ""),
            notes=crm_data.get("notes", ""),
            status=crm_data.get("status", "unbekannt"),
            calendly_link=crm_data.get("calendly_link"),
            analysis=analysis,
        )

    await audio_recorder.notify_call_end()
    audio_recorder.stop()
    try:
        rec = audio_recorder.save(directory="sessions", timestamp=session_timestamp)
        if rec:
            logger.info("Audio gespeichert: %s", rec.get("recording"))
    except Exception as e:
        logger.error("Audio-Speicherung fehlgeschlagen: %s", e)
    await audio_recorder.close()


async def end_call_monitor(ctx, finalize_fn, session):
    """Überwacht call_ended Event und beendet den SIP-Call."""
    from tool_handler import call_ended

    reason = "unbekannt"
    try:
        logger.info("End-Call Monitor aktiviert.")
        await asyncio.wait_for(call_ended.wait(), timeout=600)
        reason = "end_call aufgerufen"
        logger.info("End-Call Signal empfangen!")
    except asyncio.TimeoutError:
        reason = "timeout (10min)"
        logger.warning("Call timeout nach 10 Minuten.")
    except Exception as e:
        reason = f"fehler: {e}"
        logger.error("Fehler im End-Call Monitor: %s", e, exc_info=True)
    finally:
        try:
            await finalize_fn(reason)
        except Exception as e:
            logger.error("finalize_session fehlgeschlagen: %s", e, exc_info=True)

        # SIP-Participant entfernen
        try:
            from livekit.api import LiveKitAPI, RoomParticipantIdentity
            lk_url = os.getenv("LIVEKIT_URL", "")
            lk_key = os.getenv("LIVEKIT_API_KEY", "")
            lk_secret = os.getenv("LIVEKIT_API_SECRET", "")
            async with LiveKitAPI(lk_url, lk_key, lk_secret) as lk:
                for identity in list(ctx.room.remote_participants):
                    logger.info("Entferne Participant %s", identity)
                    await lk.room.remove_participant(
                        RoomParticipantIdentity(room=ctx.room.name, identity=identity)
                    )
        except Exception as e:
            logger.warning("SIP-Auflegen fehlgeschlagen: %s", e)
            try:
                await ctx.room.disconnect()
            except Exception:
                pass

        session.shutdown(drain=False)
        logger.info("Session beendet.")
