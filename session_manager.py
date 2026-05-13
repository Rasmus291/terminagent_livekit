"""Session-Lifecycle: Event-Handler, Finalization und Call-Control."""

import asyncio
import datetime
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

_START_TRIGGER_PREFIX = "[START_TRIGGER]"

# Audio-Latenz-Tracking (User stoppt → Agent spricht)
_user_stopped_speaking_at: float | None = None
_audio_latencies: list[float] = []
_last_agent_speech_time: float = 0.0


def get_last_agent_speech_time() -> float:
    return _last_agent_speech_time


def register_audio_latency_events(session):
    """Registriert Events zur echten Audio-Latenz-Messung."""
    global _user_stopped_speaking_at, _audio_latencies, _last_agent_speech_time
    _user_stopped_speaking_at = None
    _audio_latencies = []
    _last_agent_speech_time = 0.0

    def on_user_state(event):
        global _user_stopped_speaking_at
        if event.new_state == "listening" and event.old_state == "speaking":
            _user_stopped_speaking_at = event.created_at

    def on_agent_state(event):
        global _user_stopped_speaking_at
        if event.new_state == "speaking" and event.old_state in ("thinking", "listening"):
            if _user_stopped_speaking_at is not None:
                latency = event.created_at - _user_stopped_speaking_at
                if 0 < latency < 30:  # plausibel
                    _audio_latencies.append(latency)
                    logger.info("🎙️ Audio-Latenz: %.2fs (User→Agent)", latency)
                    # An Monitor-API senden
                    asyncio.create_task(_send_latency(latency))
                _user_stopped_speaking_at = None

    session.on("user_state_changed", on_user_state)
    session.on("agent_state_changed", on_agent_state)


async def _send_latency(latency: float):
    """Sendet Latenz-Messung an die Monitor-API."""
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                "http://localhost:8080/monitor/latency",
                json={"latency": round(latency, 3), "avg": round(sum(_audio_latencies) / len(_audio_latencies), 3) if _audio_latencies else latency},
                timeout=2,
            )
    except Exception:
        pass


def get_audio_latencies() -> list[float]:
    return list(_audio_latencies)


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

    # Track user utterances to avoid duplicate entries from multiple event sources
    _seen_user_texts: set[str] = set()

    def _extract_text(item) -> str | None:
        """Extract text from a ChatMessage, checking both str content and AudioContent.transcript."""
        text = getattr(item, "text_content", None)
        if text:
            return text
        # For realtime audio with transcription, text is in AudioContent.transcript
        content = getattr(item, "content", None)
        if content:
            parts = []
            for c in content:
                t = getattr(c, "transcript", None)
                if t:
                    parts.append(t)
            if parts:
                return " ".join(parts)
        return None

    def on_conversation_item(event):
        item = getattr(event, "item", None)
        if not item:
            return
        role = getattr(item, "role", None)
        text = _extract_text(item)
        if not text:
            return

        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        now_perf = time.perf_counter()

        if role == "user":
            if text.startswith(_START_TRIGGER_PREFIX):
                return
            _seen_user_texts.add(text)
            last_user_speech_end[0] = now_perf
            logger.info("User: %s", text)
            session_transcript.append(f"**[{ts}] User:** {text}")

            # Mailbox-Erkennung: sofort auflegen
            normalized_lower = text.lower()
            mailbox_keywords = ("mailbox", "anrufbeantworter", "hinterlassen sie eine nachricht",
                                "nach dem signalton", "piep", "zur zeit nicht erreichbar",
                                "derzeit nicht erreichbar", "momentan nicht erreichbar")
            if any(kw in normalized_lower for kw in mailbox_keywords):
                from tool_handler import call_ended as _ce
                logger.info("Mailbox erkannt — lege sofort auf.")
                if _ce and not _ce.is_set():
                    _ce.set()
                return

            mark_partner_farewell(text)
        elif role == "assistant":
            global _last_agent_speech_time
            _last_agent_speech_time = now_perf
            if last_user_speech_end[0] is not None:
                latency = now_perf - last_user_speech_end[0]
                latencies_list.append(latency)
                logger.info("⏱️ Antwort-Latenz: %.2fs", latency)
                last_user_speech_end[0] = None
            started_event.set()
            logger.info("Agent: %s", text)
            session_transcript.append(f"**[{ts}] Agent:** {text}")
            mark_assistant_farewell(text)

            # Farewell: Agent hat sich verabschiedet → sehr kurz warten, dann auflegen
            from tool_handler import assistant_farewell_detected, partner_farewell_detected, call_ended
            if assistant_farewell_detected and call_ended and not call_ended.is_set():
                if partner_farewell_detected:
                    # Partner hat sich SCHON verabschiedet → sofort auflegen
                    logger.info("Beide verabschiedet — lege sofort auf.")
                    call_ended.set()
                else:
                    # Warte max 0.5s auf Partner-Verabschiedung
                    async def _farewell_timer():
                        for _ in range(5):  # 5 * 100ms = 0.5s
                            await asyncio.sleep(0.1)
                            if partner_farewell_detected or call_ended.is_set():
                                break
                        if not call_ended.is_set():
                            logger.info("Auflegen nach Verabschiedung (0.5s Timer).")
                            call_ended.set()
                    asyncio.create_task(_farewell_timer())

        # Partner verabschiedet sich → sofort auflegen (Agent hat schon gesprochen)
        if role == "user":
            from tool_handler import assistant_farewell_detected as _asst_fw, partner_farewell_detected as _part_fw, call_ended as _ce
            if _part_fw and _asst_fw and _ce and not _ce.is_set():
                logger.info("Beide verabschiedet — lege sofort auf.")
                _ce.set()

    def on_user_input_transcribed(event):
        """Fallback handler for user_input_transcribed events (when conversation_item_added lacks text)."""
        if not event.is_final:
            return
        text = (event.transcript or "").strip()
        if not text or text.startswith(_START_TRIGGER_PREFIX):
            return
        # Avoid duplicate if already captured via conversation_item_added
        if text in _seen_user_texts:
            return
        _seen_user_texts.add(text)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info("User (transcribed): %s", text)
        session_transcript.append(f"**[{ts}] User:** {text}")
        mark_partner_farewell(text)

    return on_conversation_item, on_user_input_transcribed


async def finalize_session(
    session_transcript, crm_data, audio_recorder,
    session_start_time, session_start_perf, session_timestamp, latencies,
):
    """Generiert Analyse, speichert Report, sendet E-Mail, speichert Audio."""
    from reporting import generate_analysis, save_session_report

    # SOFORT UI benachrichtigen (bevor Analyse läuft)
    await audio_recorder.notify_call_end()

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

    # E-Mail immer senden (basierend auf Analyse)
    try:
        import email_service
        email_service.send_call_result_summary(
            call_start_time=call_start_str,
            call_duration_seconds=call_duration,
            crm_data=crm_data,
            analysis=analysis,
            transcript=session_transcript,
        )
    except Exception as e:
        logger.error("E-Mail-Versand fehlgeschlagen: %s", e)

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
    import tool_handler

    # Event wurde bereits in lavita_agent() im richtigen Loop erstellt.
    # Falls es noch None ist (sollte nicht passieren), erstelle es hier.
    if tool_handler.call_ended is None:
        tool_handler.call_ended = asyncio.Event()
    call_ended_event = tool_handler.call_ended

    reason = "unbekannt"
    try:
        logger.info("End-Call Monitor aktiviert.")
        await asyncio.wait_for(call_ended_event.wait(), timeout=600)
        reason = "end_call aufgerufen"
        logger.info("End-Call Signal empfangen!")
    except asyncio.TimeoutError:
        reason = "timeout (10min)"
        logger.warning("Call timeout nach 10 Minuten.")
    except Exception as e:
        reason = f"fehler: {e}"
        logger.error("Fehler im End-Call Monitor: %s", e, exc_info=True)
    finally:
        # Sofort Session unterbrechen damit Agent nichts mehr sagt
        try:
            if session:
                session.interrupt()
        except Exception:
            pass

        try:
            await finalize_fn(reason)
        except Exception as e:
            logger.error("finalize_session fehlgeschlagen: %s", e, exc_info=True)

        # SIP-Participant entfernen mit Retry
        try:
            from livekit.api import LiveKitAPI, RoomParticipantIdentity
            lk_url = os.getenv("LIVEKIT_URL", "")
            lk_key = os.getenv("LIVEKIT_API_KEY", "")
            lk_secret = os.getenv("LIVEKIT_API_SECRET", "")
            async with LiveKitAPI(lk_url, lk_key, lk_secret) as lk:
                for identity in list(ctx.room.remote_participants):
                    logger.info("Entferne Participant %s", identity)
                    for _attempt in range(3):
                        try:
                            await lk.room.remove_participant(
                                RoomParticipantIdentity(room=ctx.room.name, identity=identity)
                            )
                            break
                        except Exception as _e:
                            if _attempt == 2:
                                logger.warning("Participant %s konnte nicht entfernt werden: %s", identity, _e)
                            else:
                                await asyncio.sleep(1.0 * (2 ** _attempt))
        except Exception as e:
            logger.warning("SIP-Auflegen fehlgeschlagen: %s", e)
            try:
                await ctx.room.disconnect()
            except Exception:
                pass

        session.shutdown(drain=False)
        # Kurzer Delay vor dem endgültigen Exit — gibt dem Rust WebRTC-Layer
        # Zeit, ausstehende IO-Operationen sauber abzuschließen.
        # Verhindert den "ParseIntError" Rust Panic beim Cleanup.
        await asyncio.sleep(1.0)
        try:
            await ctx.room.disconnect()
        except Exception:
            pass
        logger.info("Session beendet.")
