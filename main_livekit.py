import asyncio
import datetime
import logging
import os
import struct
import sys
import threading
import time
import wave

from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, RunContext, function_tool
from livekit.plugins import google, silero

from config import GEMINI_API_KEY, MODEL_ID, SYSTEM_INSTRUCTION
from reporting_livekit import build_learning_brief, generate_analysis, save_session_report
from tool_handler_livekit import (
    call_ended,
    check_availability,
    crm_data_saved,
    end_call,
    mark_assistant_farewell,
    mark_partner_farewell,
    reset_call_state,
    schedule_appointment,
)

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_START_TRIGGER_PREFIX = "[START_TRIGGER]"
_RECORDING_SAMPLE_RATE = 16000


class RoomAudioRecorder:
    """Zeichnet Audio aus dem LiveKit Room auf (Partner + Agent als Stereo-WAV)."""

    def __init__(self, sample_rate: int = _RECORDING_SAMPLE_RATE):
        self.sample_rate = sample_rate
        self._partner_frames: list[bytes] = []
        self._agent_frames: list[bytes] = []
        self._recording = False
        self._partner_task: asyncio.Task | None = None
        self._agent_task: asyncio.Task | None = None
        self._room: rtc.Room | None = None

    def start(self, room: rtc.Room):
        """Startet Aufnahme: Lauscht auf Remote-Tracks (Partner) und lokale Tracks (Agent)."""
        self._recording = True
        self._room = room

        @room.on("track_subscribed")
        def _on_remote_track(track: rtc.Track, publication: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant):
            if track.kind == rtc.TrackKind.KIND_AUDIO and self._partner_task is None:
                logger.info("Partner-Audio-Track abonniert (%s) — starte Aufnahme.", participant.identity)
                self._partner_task = asyncio.create_task(self._capture_loop(track, self._partner_frames, "Partner"))

        @room.on("local_track_published")
        def _on_local_track(publication: rtc.LocalTrackPublication, track: rtc.Track):
            if track.kind == rtc.TrackKind.KIND_AUDIO and self._agent_task is None:
                logger.info("Agent-Audio-Track veröffentlicht — starte Aufnahme.")
                self._agent_task = asyncio.create_task(self._capture_loop(track, self._agent_frames, "Agent"))

    async def _capture_loop(self, track: rtc.Track, frame_list: list[bytes], label: str):
        try:
            stream = rtc.AudioStream(track, sample_rate=self.sample_rate, num_channels=1)
            async for event in stream:
                if not self._recording:
                    break
                frame_list.append(event.frame.data.tobytes())
        except Exception as e:
            logger.warning("%s-Audio-Capture abgebrochen: %s", label, e)
        finally:
            logger.info("%s-Audio-Capture beendet. Frames: %d", label, len(frame_list))

    def stop(self):
        self._recording = False
        for task in (self._partner_task, self._agent_task):
            if task and not task.done():
                task.cancel()

    def save(self, directory: str = "sessions", timestamp: str | None = None) -> dict:
        """Speichert aufgenommenes Audio als Stereo-WAV (Partner links, Agent rechts)."""
        if not self._partner_frames and not self._agent_frames:
            logger.info("Keine Audio-Frames zum Speichern.")
            return {}

        os.makedirs(directory, exist_ok=True)
        timestamp = timestamp or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        partner_data = b"".join(self._partner_frames)
        agent_data = b"".join(self._agent_frames)

        # Auf gleiche Länge bringen (kürzeren mit Stille auffüllen)
        max_len = max(len(partner_data), len(agent_data))
        partner_data = partner_data.ljust(max_len, b"\x00")
        agent_data = agent_data.ljust(max_len, b"\x00")

        # Stereo interleaven: [Partner_sample, Agent_sample, ...]
        num_samples = max_len // 2
        partner_samples = struct.unpack(f"<{num_samples}h", partner_data[:num_samples * 2])
        agent_samples = struct.unpack(f"<{num_samples}h", agent_data[:num_samples * 2])

        stereo = bytearray()
        for p, a in zip(partner_samples, agent_samples):
            stereo.extend(struct.pack("<hh", p, a))

        path = os.path.join(directory, f"recording_{timestamp}.wav")
        with wave.open(path, "wb") as wf:
            wf.setnchannels(2)  # Stereo
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(self.sample_rate)
            wf.writeframes(bytes(stereo))

        duration = num_samples / self.sample_rate
        logger.info("Stereo-Audio gespeichert: %s (%.1fs, Partner: %d Frames, Agent: %d Frames)",
                     path, duration, len(self._partner_frames), len(self._agent_frames))
        return {"recording": path, "duration_seconds": round(duration, 1)}


class LaVitaLiveKitAgent(Agent):
    def __init__(self, instructions: str):
        super().__init__(instructions=instructions)

    @function_tool()
    async def check_availability(self, context: RunContext, days_ahead: int = 5) -> dict:
        """Prüft verfügbare Terminslots in Calendly für die nächsten Tage (1-7)."""
        return await check_availability(days_ahead=days_ahead)

    @function_tool()
    async def schedule_appointment(
        self,
        context: RunContext,
        partner_name: str,
        status: str,
        appointment_date: str = "",
        contact_method: str = "",
        notes: str = "",
    ) -> dict:
        """Speichert Termindaten, erstellt optional einen Calendly-Link und versendet eine Benachrichtigung."""
        context.disallow_interruptions()
        return await schedule_appointment(
            partner_name=partner_name,
            status=status,
            appointment_date=appointment_date,
            contact_method=contact_method,
            notes=notes,
        )

    @function_tool()
    async def end_call(self, context: RunContext, reason: str) -> dict:
        """Beendet das Gespräch aktiv nach der finalen Verabschiedung."""
        return await end_call(reason=reason)


server = AgentServer()


@server.rtc_session(agent_name=os.getenv("LIVEKIT_AGENT_NAME", "lavita-agent"))
async def lavita_agent(ctx: JobContext):
    if not GEMINI_API_KEY:
        logger.error("API-Key fehlt! Bitte GEMINI_API_KEY in der .env setzen.")
        return

    os.makedirs("sessions", exist_ok=True)
    reset_call_state()

    session_transcript: list[str] = []
    session_start_time = datetime.datetime.now()
    session_start_perf = time.perf_counter()
    session_timestamp = session_start_time.strftime("%Y%m%d_%H%M%S")
    assistant_started_event = asyncio.Event()
    audio_recorder = RoomAudioRecorder()

    # Latenz-Tracking
    _last_user_speech_end: float | None = None
    _latencies: list[float] = []

    # Calendly-Verfügbarkeit vorab cachen (spart 2-3s bei check_availability)
    _cached_availability: str = ""
    try:
        import calendly_service
        slots_text = await calendly_service.format_available_slots(days_ahead=5)
        if slots_text and "Keine freien" not in slots_text:
            _cached_availability = f"\n\nVERFÜGBARE TERMINE (vorab geladen):\n{slots_text}"
            logger.info("Calendly-Verfügbarkeit vorab gecacht.")
    except Exception as e:
        logger.warning("Calendly-Vorab-Cache fehlgeschlagen: %s", e)

    runtime_instruction = (
        f"{SYSTEM_INSTRUCTION}{_cached_availability}\n\n"
        "AKTUELLER KONTEXT: Der Partner ist bereits in der Leitung. "
        "Beginne jetzt proaktiv mit Begrüßung und kurzem Anliegen. "
        "Mache noch keinen konkreten Terminslot in der ersten Aussage."
    )

    agent = LaVitaLiveKitAgent(instructions=runtime_instruction)
    session = AgentSession(
        llm=google.realtime.RealtimeModel(
            model=MODEL_ID,
            voice=os.getenv("LIVEKIT_GEMINI_VOICE", "Kore"),
            api_key=GEMINI_API_KEY,
            instructions=runtime_instruction,
            language="de-DE",
        ),
        vad=silero.VAD.load(
            min_silence_duration=0.25,
            min_speech_duration=0.1,
            prefix_padding_duration=0.2,
        ),
        turn_handling={
            "turn_detection": "realtime_llm",
            "endpointing": {
                "mode": "dynamic",
                "min_delay": 0.15,
                "max_delay": 0.8,
            },
        },
    )

    def on_conversation_item(event):
        nonlocal _last_user_speech_end
        item = getattr(event, "item", None)
        if not item:
            logger.debug("on_conversation_item: Item ist None")
            return
        role = getattr(item, "role", None)
        text = getattr(item, "text_content", None)
        if not text:
            logger.debug(f"on_conversation_item: Text ist None (role={role})")
            return

        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        now_perf = time.perf_counter()
        if role == "user":
            if text.startswith(_START_TRIGGER_PREFIX):
                logger.info("Interner Start-Trigger gesendet.")
                return
            _last_user_speech_end = now_perf
            logger.info("User: %s", text)
            session_transcript.append(f"**[{ts}] User:** {text}")
            logger.info("Rufe mark_partner_farewell auf mit: '%s'", text)
            mark_partner_farewell(text)
        elif role == "assistant":
            # Latenz messen: Zeit zwischen User-Ende und Agent-Antwort
            if _last_user_speech_end is not None:
                latency = now_perf - _last_user_speech_end
                _latencies.append(latency)
                logger.info("⏱️ Antwort-Latenz: %.2fs", latency)
                _last_user_speech_end = None
            assistant_started_event.set()
            logger.info("Agent: %s", text)
            session_transcript.append(f"**[{ts}] Agent:** {text}")
            mark_assistant_farewell(text)

    session.on("conversation_item_added", on_conversation_item)

    def on_session_close(event):
        """Wird aufgerufen wenn die AgentSession geschlossen wird (z.B. Participant disconnect)."""
        reason = getattr(event, "reason", "unbekannt")
        logger.info("Session geschlossen (Grund: %s). Setze call_ended Event.", reason)
        if not call_ended.is_set():
            call_ended.set()

    session.on("close", on_session_close)

    _finalize_done = False

    async def finalize_session(reason: str = ""):
        nonlocal _finalize_done
        if _finalize_done:
            logger.info("finalize_session bereits ausgeführt, überspringe.")
            return
        _finalize_done = True

        call_duration = time.perf_counter() - session_start_perf
        call_start_str = session_start_time.strftime("%Y-%m-%d %H:%M:%S")

        logger.info("Generiere Analyse + Sentiment...")

        # Latenz-Statistik loggen
        if _latencies:
            avg_lat = sum(_latencies) / len(_latencies)
            min_lat = min(_latencies)
            max_lat = max(_latencies)
            logger.info("⏱️ Latenz-Statistik: Ø %.2fs | Min %.2fs | Max %.2fs | %d Turns",
                         avg_lat, min_lat, max_lat, len(_latencies))
        try:
            analysis = await asyncio.to_thread(generate_analysis, session_transcript)
        except Exception as e:
            logger.error("Analyse fehlgeschlagen: %s", e, exc_info=True)
            analysis = {
                "zusammenfassung": f"*Analyse-Fehler: {e}*",
                "sentiment_partner": None,
                "sentiment_gesamt": "unbekannt",
                "stimmung_details": "",
                "ergebnis": "unbekannt",
            }

        logger.info("Speichere Session Report...")
        save_session_report(
            session_transcript,
            crm_data=crm_data_saved or None,
            call_duration=call_duration,
            call_start_time=call_start_str,
            analysis=analysis,
            timestamp=session_timestamp,
        )

        # E-Mail mit Gesprächsergebnis + Analyse versenden
        if crm_data_saved:
            import email_service

            email_service.send_appointment_proposal(
                partner_name=crm_data_saved.get("partner_name", "Unbekannt"),
                appointment_date=crm_data_saved.get("appointment_date", ""),
                notes=crm_data_saved.get("notes", ""),
                status=crm_data_saved.get("status", "unbekannt"),
                calendly_link=crm_data_saved.get("calendly_link"),
                analysis=analysis,
            )

        # Audio-Aufnahme speichern
        audio_recorder.stop()
        try:
            rec_result = audio_recorder.save(directory="sessions", timestamp=session_timestamp)
            if rec_result:
                logger.info("Audio gespeichert: %s", rec_result.get("recording"))
        except Exception as e:
            logger.error("Audio-Speicherung fehlgeschlagen: %s", e)

        if reason:
            logger.info("Session beendet (%s).", reason)

    ctx.add_shutdown_callback(finalize_session)

    logger.info("Verbinde LiveKit Room + Gemini Live Modell...")
    await ctx.connect()
    audio_recorder.start(ctx.room)
    job_id = str(getattr(getattr(ctx, "job", None), "id", ""))
    is_console_job = job_id.startswith("mock-job")
    participant_wait_timeout = float(os.getenv("LIVEKIT_WAIT_PARTICIPANT_SECS", "45"))
    if is_console_job:
        logger.info("Console/Mock-Job erkannt (%s) – starte ohne Teilnehmer-Wartezeit.", job_id)
    else:
        try:
            logger.info("Warte auf verbundenen Teilnehmer (timeout=%.0fs)...", participant_wait_timeout)
            participant = await asyncio.wait_for(ctx.wait_for_participant(), timeout=participant_wait_timeout)
            logger.info("Teilnehmer verbunden: identity=%s", participant.identity)
        except asyncio.TimeoutError:
            logger.warning("Kein Teilnehmer innerhalb von %.0fs erkannt. Starte trotzdem Session.", participant_wait_timeout)

    async def end_call_monitor():
        """Überwacht ob end_call() aufgerufen wurde und beendet dann die Session."""
        reason = "unbekannt"
        try:
            logger.info("End-Call Monitor aktiviert. Warte auf Auflegen-Signal...")
            # Timeout auf 10 Minuten setzen um zu lange Calls zu vermeiden
            await asyncio.wait_for(call_ended.wait(), timeout=600)
            reason = "end_call aufgerufen"
            logger.info("End-Call Signal empfangen!")
        except asyncio.TimeoutError:
            reason = "timeout (10min)"
            logger.warning("Call timeout nach 10 Minuten erreicht.")
        except Exception as e:
            reason = f"fehler: {e}"
            logger.error(f"Fehler im End-Call Monitor: {e}", exc_info=True)
        finally:
            # WICHTIG: finalize_session EXPLIZIT aufrufen, bevor Session heruntergefahren wird
            logger.info("Starte finalize_session (Grund: %s)...", reason)
            try:
                await finalize_session(reason)
            except Exception as e:
                logger.error("finalize_session fehlgeschlagen: %s", e, exc_info=True)

            # SIP-Call aktiv auflegen: Room disconnecten bevor Session shutdown
            logger.info("Trenne Room-Verbindung (legt SIP-Call auf)...")
            try:
                await ctx.room.disconnect()
                logger.info("Room getrennt — SIP-Call aufgelegt.")
            except Exception as e:
                logger.warning("Room-Disconnect fehlgeschlagen: %s", e)

            logger.info("Fahre Session herunter (drain=False)...")
            session.shutdown(drain=False)
            logger.info("Session beendet - bereit für nächsten Call.")

    async def run_session():
        """Startet die Session und stößt die Gesprächseröffnung genau einmal aktiv an."""
        logger.info("Starte Gemini Live Session...")
        try:
            await session.start(room=ctx.room, agent=agent)
            logger.info("Session erfolgreich gestartet.")
        except Exception as e:
            logger.error(f"Fehler beim Session-Start: {e}", exc_info=True)
            raise

        # Kurz warten damit Gemini-WebSocket bereit ist
        await asyncio.sleep(0.5)
        try:
            logger.info("Stoße Gesprächseröffnung einmalig an...")
            session.generate_reply(
                user_input=f"{_START_TRIGGER_PREFIX} Beginne jetzt das Gespräch.",
            )
        except Exception as e:
            logger.warning("Gesprächseröffnung per generate_reply() fehlgeschlagen: %s", e)

    # Starte Session und End-Call Monitor parallel
    logger.info("Starte Agent-Loop (session + end_call_monitor)...")
    try:
        await asyncio.gather(
            run_session(),
            end_call_monitor(),
            return_exceptions=False
        )
    except Exception as e:
        logger.error(f"Fehler im Agent-Loop: {e}", exc_info=True)
    finally:
        logger.info("Agent-Loop beendet.")


if __name__ == "__main__":
    agents.cli.run_app(server)
