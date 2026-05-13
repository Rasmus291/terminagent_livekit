import asyncio
import base64
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
from livekit.plugins.google.beta import realtime as google_realtime
from google.genai import types as genai_types
# Pipeline-Imports (auskommentiert für spätere Nutzung):
# from livekit.plugins import deepgram, elevenlabs, google, silero

from config import GEMINI_API_KEY, SYSTEM_INSTRUCTION, GEMINI_VOICE, GEMINI_LIVE_MODEL
from reporting import build_learning_brief, generate_analysis, save_session_report
import tool_handler
from tool_handler import CallState

load_dotenv()
# os.environ.setdefault("LK_GOOGLE_DEBUG", "1")  # Debug: deaktiviert für Performance

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _kill_zombie_workers():
    """Killt alle alten Python-Prozesse die main_livekit.py ausführen."""
    import psutil
    my_pid = os.getpid()
    # Alle eigenen Kinder (Worker) ermitteln
    try:
        me = psutil.Process(my_pid)
        my_children_pids = {c.pid for c in me.children(recursive=True)}
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        my_children_pids = set()

    killed = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            pid = proc.info["pid"]
            if pid == my_pid or pid in my_children_pids:
                continue
            cmdline = proc.info.get("cmdline") or []
            cmdline_str = " ".join(cmdline).lower()
            if "main_livekit.py" in cmdline_str and "python" in (proc.info.get("name") or "").lower():
                proc.kill()
                killed.append(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if killed:
        logger.info("🧹 Zombie-Workers gekillt: %s", killed)


_START_TRIGGER_PREFIX = "[START_TRIGGER]"
_RECORDING_SAMPLE_RATE = 16000


class RoomAudioRecorder:
    """Zeichnet Audio aus dem LiveKit Room auf (Partner + Agent als Stereo-WAV)
    und streamt es optional an den Live-Monitor."""

    MONITOR_URL = os.getenv("MONITOR_API_URL", "http://localhost:8080")
    MONITOR_CHUNK_FRAMES = 20  # Alle N Frames an Monitor senden (~1s bei 50ms Frames) — weniger Overhead

    def __init__(self, sample_rate: int = _RECORDING_SAMPLE_RATE):
        self.sample_rate = sample_rate
        self._partner_frames: list[bytes] = []
        self._agent_frames: list[bytes] = []
        self._recording = False
        self._partner_task: asyncio.Task | None = None
        self._agent_task: asyncio.Task | None = None
        self._room: rtc.Room | None = None
        self._http_session: "aiohttp.ClientSession | None" = None

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

        # Bereits abonnierte Remote-Tracks sofort erfassen
        for participant in room.remote_participants.values():
            for pub in participant.track_publications.values():
                track = pub.track
                if track and track.kind == rtc.TrackKind.KIND_AUDIO and self._partner_task is None:
                    logger.info("Bestehender Partner-Audio-Track gefunden (%s)", participant.identity)
                    self._partner_task = asyncio.create_task(self._capture_loop(track, self._partner_frames, "Partner"))

    async def _capture_loop(self, track: rtc.Track, frame_list: list[bytes], label: str):
        track_name = "partner" if label == "Partner" else "agent"
        monitor_buf: list[bytes] = []
        try:
            stream = rtc.AudioStream(track, sample_rate=self.sample_rate, num_channels=1)
            async for event in stream:
                if not self._recording:
                    break
                raw = event.frame.data.tobytes()
                frame_list.append(raw)

                # Live-Monitor: sammle Frames und sende gebündelt (fire-and-forget)
                monitor_buf.append(raw)
                if len(monitor_buf) >= self.MONITOR_CHUNK_FRAMES:
                    asyncio.create_task(self._send_to_monitor(track_name, b"".join(monitor_buf)))
                    monitor_buf.clear()
        except Exception as e:
            logger.warning("%s-Audio-Capture abgebrochen: %s", label, e)
        finally:
            if monitor_buf:
                asyncio.create_task(self._send_to_monitor(track_name, b"".join(monitor_buf)))
            logger.info("%s-Audio-Capture beendet. Frames: %d", label, len(frame_list))

    async def _send_to_monitor(self, track: str, pcm_data: bytes):
        """Sendet Audio-Chunk an den Monitor-API-Server (fire-and-forget)."""
        import aiohttp as _aiohttp
        try:
            if self._http_session is None:
                self._http_session = _aiohttp.ClientSession()
            b64 = base64.b64encode(pcm_data).decode("ascii")
            async with self._http_session.post(
                f"{self.MONITOR_URL}/monitor/audio",
                json={"track": track, "sample_rate": self.sample_rate, "pcm16_b64": b64},
                timeout=_aiohttp.ClientTimeout(total=1),
            ) as resp:
                pass
        except Exception:
            pass

    def stop(self):
        self._recording = False
        for task in (self._partner_task, self._agent_task):
            if task and not task.done():
                task.cancel()

    async def close(self):
        """Schließt HTTP-Session."""
        self.stop()
        if self._http_session:
            await self._http_session.close()
            self._http_session = None

    async def notify_call_start(self, contact_name: str = ""):
        """Informiert den Monitor über einen neuen Anruf."""
        await self._send_call_state({"event": "call-start", "active": True, "contact_name": contact_name})

    async def notify_call_end(self):
        """Informiert den Monitor über ein Call-Ende."""
        await self._send_call_state({"event": "call-end", "active": False})

    async def _send_latency(self, latency: float, avg: float):
        """Sendet Latenz-Messung an den Monitor-API-Server."""
        import aiohttp as _aiohttp
        try:
            if self._http_session is None:
                self._http_session = _aiohttp.ClientSession()
            logger.info("📡 Sende Latenz an Monitor: %.2fs (avg: %.2fs)", latency, avg)
            async with self._http_session.post(
                f"{self.MONITOR_URL}/monitor/latency",
                json={"latency": latency, "avg": avg},
                timeout=_aiohttp.ClientTimeout(total=2),
            ) as resp:
                logger.info("📡 Latenz an Monitor gesendet: Status %d", resp.status)
        except Exception as e:
            logger.warning("📡 Latenz senden fehlgeschlagen: %s", e)

    async def _send_call_state(self, state: dict):
        import aiohttp as _aiohttp
        try:
            if self._http_session is None:
                self._http_session = _aiohttp.ClientSession()
            async with self._http_session.post(
                f"{self.MONITOR_URL}/monitor/call-state",
                json=state,
                timeout=_aiohttp.ClientTimeout(total=1),
            ) as resp:
                pass
        except Exception:
            pass

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

        # Vorallokierter Buffer statt bytearray.extend() in Schleife (deutlich schneller bei langen Calls)
        stereo = bytearray(num_samples * 4)
        struct.pack_into(f"<{num_samples * 2}h", stereo, 0,
                         *[v for pair in zip(partner_samples, agent_samples) for v in pair])

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
    def __init__(self, instructions: str, state: CallState):
        super().__init__(instructions=instructions)
        self._state = state

    @function_tool()
    async def end_call(self, context: RunContext, reason: str) -> dict:
        """Beendet das Gespräch aktiv nach der finalen Verabschiedung."""
        return await tool_handler.end_call(self._state, reason=reason)


server = AgentServer(num_idle_processes=2)


@server.rtc_session(agent_name=os.getenv("LIVEKIT_AGENT_NAME", "lavita-agent"))
async def lavita_agent(ctx: JobContext):
    if not GEMINI_API_KEY:
        logger.error("API-Key fehlt! Bitte GEMINI_API_KEY in der .env setzen.")
        return

    os.makedirs("sessions", exist_ok=True)
    # Frischer Zustand pro Anruf — kein Überbleibsel aus vorherigen Calls möglich
    state = CallState()

    session_transcript: list[str] = []
    session_start_time = datetime.datetime.now()
    session_start_perf = time.perf_counter()
    session_timestamp = session_start_time.strftime("%Y%m%d_%H%M%S")
    assistant_started_event = asyncio.Event()
    audio_recorder = RoomAudioRecorder()

    # Latenz-Tracking
    from latency_profiler import LatencyProfiler
    _latency_profiler = LatencyProfiler()
    _latencies: list[float] = []
    _last_user_transcript_time: float = 0.0  # Fallback-Latenz: Transkript-basiert

    runtime_instruction = SYSTEM_INSTRUCTION
    # Inject current date/time so agent knows what day it is
    today = datetime.datetime.now()
    weekdays_de = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
    date_info = f"\n\nAKTUELLES DATUM: {weekdays_de[today.weekday()]}, {today.strftime('%d.%m.%Y')}. Berechne Terminvorschläge relativ zu diesem Datum.\n"
    runtime_instruction = runtime_instruction + date_info

    agent = LaVitaLiveKitAgent(instructions=runtime_instruction, state=state)

    def on_conversation_item(event):
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
            logger.info("User: %s", text)
            session_transcript.append(f"**[{ts}] User:** {text}")
            nonlocal _last_user_transcript_time
            _last_user_transcript_time = now_perf
            # Mailbox-Check VOR Farewell-Check: Mailboxen sagen oft "auf Wiederhören" o.ä.
            _mailbox_keywords = ["mobilbox", "mailbox", "anrufbeantworter", "voicemail",
                                 "hinterlassen sie", "nach dem ton", "nach dem signal"]
            if any(kw in text.lower() for kw in _mailbox_keywords):
                logger.info("📞 Mailbox erkannt — lege sofort auf.")
                if not state.call_ended.is_set():
                    state.call_ended.set()
                return  # Kein Farewell-Check bei Mailbox
            tool_handler.mark_partner_farewell(state, text)
        elif role == "assistant":
            # Fallback-Latenz: Transkript-zu-Transkript (wenn LatencyProfiler nicht feuert)
            if _last_user_transcript_time > 0:
                fallback_latency = now_perf - _last_user_transcript_time
                if fallback_latency > 0.5 and fallback_latency < 30:
                    logger.info("⏱️ Fallback-Latenz (Transkript): %.2fs — nur intern", fallback_latency)
                    _last_user_transcript_time = 0.0
            assistant_started_event.set()
            logger.info("Agent: %s", text)
            session_transcript.append(f"**[{ts}] Agent:** {text}")
            tool_handler.mark_assistant_farewell(state, text)
            # Farewell-Timer: wenn Agent sich verabschiedet hat, nach 3s automatisch auflegen
            if state.assistant_farewell_detected and not state.call_ended.is_set():
                async def _farewell_timer():
                    await asyncio.sleep(3)
                    if not state.call_ended.is_set():
                        logger.info("⏰ Farewell-Timer abgelaufen — erzwinge Auflegen.")
                        state.call_ended.set()
                asyncio.create_task(_farewell_timer())

    def on_user_input(event):
        text = getattr(event, "transcript", "") or getattr(event, "text", "")
        is_final = getattr(event, "is_final", True)
        logger.debug("STT-Event: final=%s text='%s'", is_final, text[:50] if text else "")

    def on_session_close(event):
        """Wird aufgerufen wenn die AgentSession geschlossen wird (z.B. Participant disconnect)."""
        reason = getattr(event, "reason", "unbekannt")
        logger.info("Session geschlossen (Grund: %s). Setze call_ended Event.", reason)
        if not state.call_ended.is_set():
            state.call_ended.set()

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

        # Report + E-Mail in Thread — beides ist sync I/O (File + SMTP), darf Event Loop nicht blockieren
        _latency_snapshot = _latency_profiler.get_summary_dict()
        
        # CRM-Daten aus Analyse befüllen (Termin wird nicht mehr per Tool gebucht)
        if analysis:
            termin = analysis.get("termin", "")
            partner_name_from_analysis = analysis.get("partner_name", "")
            ergebnis = analysis.get("ergebnis", "unbekannt")
            # Status aus Ergebnis ableiten
            status_map = {"scheduled": "confirmed", "declined": "declined", "callback": "callback"}
            crm_status = status_map.get(ergebnis, ergebnis)
            if not state.crm_data.get("status"):
                state.crm_data["status"] = crm_status
            if termin and not state.crm_data.get("appointment_date"):
                state.crm_data["appointment_date"] = termin
            if partner_name_from_analysis and not state.crm_data.get("partner_name"):
                state.crm_data["partner_name"] = partner_name_from_analysis
            if not state.crm_data.get("contact_method"):
                state.crm_data["contact_method"] = "phone"
        
        _crm_snapshot = dict(state.crm_data) if state.crm_data else None

        def _sync_post_call():
            logger.info("Speichere Session Report...")
            save_session_report(
                session_transcript,
                crm_data=_crm_snapshot,
                call_duration=call_duration,
                call_start_time=call_start_str,
                analysis=analysis,
                timestamp=session_timestamp,
                latency_data=_latency_snapshot,
            )
            # Nur Email senden wenn ein sinnvoller Status vorhanden ist (nicht bei leeren/abgebrochenen Calls)
            crm_status = (_crm_snapshot.get("status") or "").strip().lower() if _crm_snapshot else ""
            if crm_status and crm_status != "unbekannt":
                import email_service
                # Transkript und Zusammenfassung in die Email einbauen
                transcript_for_email = list(session_transcript) if session_transcript else []
                email_service.send_call_result_summary(
                    call_start_time=call_start_str,
                    call_duration_seconds=call_duration,
                    crm_data=_crm_snapshot,
                    analysis=analysis,
                    transcript=transcript_for_email,
                )
            else:
                logger.info("Kein sinnvoller CRM-Status ('%s') — überspringe E-Mail.", crm_status)

        await asyncio.to_thread(_sync_post_call)

        # Audio-Aufnahme speichern & Monitor benachrichtigen
        await audio_recorder.notify_call_end()
        audio_recorder.stop()
        try:
            rec_result = await asyncio.to_thread(
                audio_recorder.save, "sessions", session_timestamp
            )
            if rec_result:
                logger.info("Audio gespeichert: %s", rec_result.get("recording"))
        except Exception as e:
            logger.error("Audio-Speicherung fehlgeschlagen: %s", e)
        await audio_recorder.close()

        if reason:
            logger.info("Session beendet (%s).", reason)

    ctx.add_shutdown_callback(finalize_session)

    # Room verbinden (Session wird NICHT sofort gestartet — erst nach 3s Delay)
    logger.info("Verbinde LiveKit Room...")
    await ctx.connect()
    audio_recorder.start(ctx.room)

    job_id = str(getattr(getattr(ctx, "job", None), "id", ""))
    is_console_job = job_id.startswith("mock-job")
    participant_wait_timeout = float(os.getenv("LIVEKIT_WAIT_PARTICIPANT_SECS", "45"))

    # Anrede aus Dispatch-Metadata lesen (gesetzt vom API-Server)
    partner_salutation = ""
    partner_name = ""
    try:
        import json as _json
        job_meta = getattr(ctx.job, "metadata", "") or ""
        if job_meta:
            meta_dict = _json.loads(job_meta)
            partner_salutation = meta_dict.get("salutation", "")
            meta_name = meta_dict.get("name", "")
            if meta_name:
                partner_name = meta_name
            logger.info("Dispatch-Metadata: name='%s', salutation='%s'", partner_name, partner_salutation)
    except Exception as e:
        logger.warning("Dispatch-Metadata konnte nicht gelesen werden: %s", e)

    if is_console_job:
        logger.info("Console/Mock-Job erkannt (%s) – starte ohne Teilnehmer-Wartezeit.", job_id)
    else:
        try:
            logger.info("Warte auf verbundenen Teilnehmer (timeout=%.0fs)...", participant_wait_timeout)
            participant = await asyncio.wait_for(ctx.wait_for_participant(), timeout=participant_wait_timeout)
            pname = getattr(participant, "name", "") or ""
            logger.info("Participant name: '%s'", pname)
            # Extrahiere echten Namen aus "Partner (Nachname)" Format
            if pname.startswith("Partner (") and pname.endswith(")"):
                extracted = pname[9:-1]
                if not extracted.startswith("+") and not extracted.startswith("phone-"):
                    partner_name = extracted
            elif pname and pname != "Partner" and not pname.startswith("phone-") and not pname.startswith("+"):
                partner_name = pname
        except asyncio.TimeoutError:
            logger.warning("Kein Teilnehmer innerhalb von %.0fs erkannt.", participant_wait_timeout)

    # notify_call_start mit contact_name
    asyncio.create_task(audio_recorder.notify_call_start(contact_name=partner_name))

    # Partner-Name in Instructions injizieren für proaktive Begrüßung
    if partner_name:
        if partner_salutation:
            name_instruction = f"\n\nWICHTIG: Der Partner heißt {partner_salutation} {partner_name}. Verwende in deiner Begrüßung EXAKT 'Hallo {partner_salutation} {partner_name}'."
        else:
            name_instruction = f"\n\nWICHTIG: Der Partner heißt {partner_name}. Verwende in deiner Begrüßung EXAKT 'Hallo {partner_name}'."
        runtime_instruction = runtime_instruction + name_instruction

    # Gemini Native Audio — kein separates STT/TTS nötig
    realtime_model = google_realtime.RealtimeModel(
        instructions=runtime_instruction,
        model=GEMINI_LIVE_MODEL,
        api_key=GEMINI_API_KEY,
        voice=GEMINI_VOICE,
        temperature=0.5,
        max_output_tokens=1000,
        proactivity=True,
        thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
        # Transcription aktivieren für Live-Transkript
        input_audio_transcription=genai_types.AudioTranscriptionConfig(),
        output_audio_transcription=genai_types.AudioTranscriptionConfig(),
        tool_response_scheduling="WHEN_IDLE",
        context_window_compression=genai_types.ContextWindowCompressionConfig(
            sliding_window=genai_types.SlidingWindow(target_tokens=1024),
        ),
        realtime_input_config=genai_types.RealtimeInputConfig(
            automaticActivityDetection=genai_types.AutomaticActivityDetection(
                endOfSpeechSensitivity=genai_types.EndSensitivity.END_SENSITIVITY_HIGH,
                silenceDurationMs=100,
            ),
        ),
    )

    session = AgentSession(
        llm=realtime_model,
        # AEC Warmup von 3.0s auf 0.5s reduzieren (-2500ms perceived latency)
        aec_warmup_duration=0.5,
        # Tool-Schritte minimieren
        max_tool_steps=1,
    )

    session.on("conversation_item_added", on_conversation_item)
    session.on("user_input_transcribed", on_user_input)
    session.on("close", on_session_close)

    # LatencyProfiler registrieren + Callback für UI
    def _on_latency(e2e: float, avg: float):
        _latencies.append(e2e)
        # Nur sinnvolle Werte an UI senden (unter 10s — darüber ist Messfehler/Tool-Call)
        if e2e < 10.0:
            # Durchschnitt nur über letzte 3 sinnvolle Werte
            recent = [l for l in _latencies[-5:] if l < 10.0]
            avg_recent = sum(recent) / len(recent) if recent else e2e
            logger.info("⏱️ E2E-Latenz: %.2fs (Ø %.2fs, letzte %d Werte)", e2e, avg_recent, len(recent))
            asyncio.create_task(audio_recorder._send_latency(e2e, avg_recent))
        else:
            logger.info("⏱️ E2E-Latenz: %.2fs — zu hoch, nicht an UI gesendet", e2e)

    def _on_ttft(ttft: float, avg_ttft: float):
        # TTFT ist bei Native Audio oft -1000ms (Sentinel) → nur loggen
        logger.info("⏱️ Gemini TTFT: %.2fs (Ø %.2fs)", ttft, avg_ttft)
    _latency_profiler.on_latency_measured = _on_latency
    _latency_profiler.on_ttft_received = _on_ttft
    _latency_profiler.register(session)

    # Session starten + kurzer Delay damit Partner Stille hört beim Abheben
    logger.info("Partner hat abgenommen — starte Gemini Native Audio Session...")

    async def _start_session():
        await session.start(
            room=ctx.room,
            agent=agent,
        )
        logger.info("Gemini Native Audio Session bereit.")
        # Warte bis RoomIO den Participant gelinkt hat
        room_io = getattr(session, '_room_io', None)
        for _ in range(20):  # max 2s warten
            if room_io and room_io.linked_participant:
                break
            await asyncio.sleep(0.1)
        logger.info("RoomIO linked_participant = %s", room_io.linked_participant if room_io else None)

    _call_start_time = time.perf_counter()
    # Erst 3.5s warten, DANN Session starten — proactivity=True lässt Gemini sonst sofort sprechen
    await asyncio.sleep(3.5)  # 3-4s Stille nach Abheben
    await _start_session()    # Session erst nach Pause starten
    elapsed = time.perf_counter() - _call_start_time
    logger.info("Session bereit nach %.1fs — sende Trigger.", elapsed)

    # Gesprächseröffnung anstoßen — Name direkt im Trigger um Halluzinationen zu vermeiden
    if partner_name and partner_salutation:
        greeting_name = f"{partner_salutation} {partner_name}"
    elif partner_name:
        greeting_name = partner_name
    else:
        greeting_name = "den Partner"
    trigger_msg = f"{_START_TRIGGER_PREFIX} Sprich jetzt deine Begrüßung. Sag 'Hallo {greeting_name}'."
    for attempt in range(3):
        try:
            logger.info("Stoße Gesprächseröffnung an (Versuch %d)...", attempt + 1)
            session.generate_reply(user_input=trigger_msg)
            break
        except Exception as e:
            logger.warning("Gesprächseröffnung Versuch %d fehlgeschlagen: %s", attempt + 1, e)
            if attempt < 2:
                await asyncio.sleep(0.3)

    async def end_call_monitor():
        """Überwacht ob end_call() aufgerufen wurde und beendet dann die Session."""
        reason = "unbekannt"
        try:
            logger.info("End-Call Monitor aktiviert. Warte auf Auflegen-Signal...")
            # Timeout auf 10 Minuten setzen um zu lange Calls zu vermeiden
            await asyncio.wait_for(state.call_ended.wait(), timeout=600)
            reason = "end_call aufgerufen"
            logger.info("End-Call Signal empfangen!")
        except asyncio.TimeoutError:
            reason = "timeout (10min)"
            logger.warning("Call timeout nach 10 Minuten erreicht.")
        except Exception as e:
            reason = f"fehler: {e}"
            logger.error(f"Fehler im End-Call Monitor: {e}", exc_info=True)
        finally:
            # 2s warten vor dem Auflegen — damit der Partner die Verabschiedung noch hört
            logger.info("Warte 2s vor dem Auflegen...")
            await asyncio.sleep(2.0)

            # SIP-Call auflegen
            logger.info("Lege SIP-Call auf...")
            try:
                from livekit.api import LiveKitAPI, RoomParticipantIdentity
                lk_url = os.getenv("LIVEKIT_URL", "")
                lk_key = os.getenv("LIVEKIT_API_KEY", "")
                lk_secret = os.getenv("LIVEKIT_API_SECRET", "")
                room_name = ctx.room.name

                async with LiveKitAPI(lk_url, lk_key, lk_secret) as lk:
                    for identity, participant in list(ctx.room.remote_participants.items()):
                        try:
                            logger.info("Entferne Participant %s aus Room %s...", identity, room_name)
                            await lk.room.remove_participant(
                                RoomParticipantIdentity(room=room_name, identity=identity)
                            )
                            logger.info("Participant %s entfernt.", identity)
                        except Exception as ep:
                            logger.debug("Participant %s bereits entfernt: %s", identity, ep)
            except Exception as e:
                logger.warning("SIP-Auflegen fehlgeschlagen: %s", e)

            # DANACH finalize_session (Report + Audio speichern)
            logger.info("Starte finalize_session (Grund: %s)...", reason)
            try:
                await finalize_session(reason)
            except Exception as e:
                logger.error("finalize_session fehlgeschlagen: %s", e, exc_info=True)

            logger.info("Session beendet - bereit für nächsten Call.")

    # Starte End-Call Monitor
    logger.info("Starte Agent-Loop (end_call_monitor)...")
    try:
        await end_call_monitor()
    except Exception as e:
        logger.error(f"Fehler im Agent-Loop: {e}", exc_info=True)
    finally:
        logger.info("Agent-Loop beendet.")


if __name__ == "__main__":
    _kill_zombie_workers()
    agents.cli.run_app(server)
