"""LiveKit Agent Entry Point — Orchestriert Session-Lifecycle."""

import asyncio
import datetime
import logging
import os
import time

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import Agent, AgentServer, AgentSession, JobContext
from livekit.plugins import google, silero
from google.genai import types as genai_types

from audio_recorder import RoomAudioRecorder
from config import GEMINI_API_KEY, MODEL_ID, SYSTEM_INSTRUCTION
from session_manager import (
    _START_TRIGGER_PREFIX,
    create_conversation_handler,
    end_call_monitor,
    finalize_session,
    register_audio_latency_events,
)
from tool_handler import (
    call_ended,
    crm_data_saved,
    mark_assistant_farewell,
    mark_partner_farewell,
    reset_call_state,
)

load_dotenv()
logger = logging.getLogger(__name__)


class LaVitaLiveKitAgent(Agent):
    def __init__(self, instructions: str):
        super().__init__(instructions=instructions)


server = AgentServer(num_idle_processes=1)


@server.rtc_session(agent_name=os.getenv("LIVEKIT_AGENT_NAME", "lavita-agent"))
async def lavita_agent(ctx: JobContext):
    if not GEMINI_API_KEY:
        logger.error("API-Key fehlt!")
        return

    os.makedirs("sessions", exist_ok=True)
    reset_call_state()

    transcript: list[str] = []
    latencies: list[float] = []
    start_time = datetime.datetime.now()
    start_perf = time.perf_counter()
    ts = start_time.strftime("%Y%m%d_%H%M%S")
    started_event = asyncio.Event()
    recorder = RoomAudioRecorder()

    # Calendly parallel laden — 2s Timeout da API variabel
    async def _load_calendly():
        try:
            import calendly_service
            slots = await calendly_service.format_available_slots(days_ahead=5)
            if slots and "Keine freien" not in slots:
                return f"\n\nVERFÜGBARE TERMINE:\n{slots}"
        except Exception:
            pass
        return ""

    calendly_task = asyncio.create_task(_load_calendly())

    _done = False

    async def _finalize(reason=""):
        nonlocal _done
        if _done:
            return
        _done = True
        await finalize_session(
            transcript, crm_data_saved or None, recorder,
            start_time, start_perf, ts, latencies,
        )

    ctx.add_shutdown_callback(_finalize)

    # ctx.connect() und Calendly parallel
    await ctx.connect()
    recorder.start(ctx.room)

    # Calendly mit Timeout — 2s sind realistischer
    try:
        cached_slots = await asyncio.wait_for(calendly_task, timeout=2.0)
    except asyncio.TimeoutError:
        cached_slots = ""
        logger.warning("Calendly-Timeout — starte ohne Slots")
    instructions = f"{SYSTEM_INSTRUCTION}{cached_slots}"

    agent = LaVitaLiveKitAgent(instructions=instructions)
    session = AgentSession(
        llm=google.realtime.RealtimeModel(
            model=MODEL_ID,
            voice=os.getenv("LIVEKIT_GEMINI_VOICE", "Kore"),
            api_key=GEMINI_API_KEY,
            instructions=instructions,
            language="de-DE",
            realtime_input_config=genai_types.RealtimeInputConfig(
                automaticActivityDetection=genai_types.AutomaticActivityDetection(
                    startOfSpeechSensitivity=genai_types.StartSensitivity.START_SENSITIVITY_MEDIUM,
                    endOfSpeechSensitivity=genai_types.EndSensitivity.END_SENSITIVITY_MEDIUM,
                    silenceDurationMs=1000, prefixPaddingMs=300,
                ),
            ),
        ),
        vad=silero.VAD.load(
            min_silence_duration=0.8, min_speech_duration=0.1,
            prefix_padding_duration=0.2, activation_threshold=0.6,
        ),
        turn_handling={
            "turn_detection": "realtime_llm",
            "endpointing": {"mode": "dynamic", "min_delay": 0.6, "max_delay": 1.2},
        },
        min_interruption_duration=0.8,
        min_interruption_words=2,
        false_interruption_timeout=1.0,
        aec_warmup_duration=0.0,
    )

    # Event-Handler registrieren
    handler, user_transcribed_handler = create_conversation_handler(transcript, latencies, started_event, {
        "mark_partner_farewell": mark_partner_farewell,
        "mark_assistant_farewell": mark_assistant_farewell,
    })
    session.on("conversation_item_added", handler)
    session.on("user_input_transcribed", user_transcribed_handler)
    register_audio_latency_events(session)

    def on_close(event):
        if not call_ended.is_set():
            call_ended.set()
    session.on("close", on_close)

    # Session sofort starten — parallel zu wait_for_participant, damit Greeting bereit ist
    session_start_task = asyncio.create_task(session.start(room=ctx.room, agent=agent))

    job_id = str(getattr(getattr(ctx, "job", None), "id", ""))
    partner_name = ""
    
    # Parallel: Warte auf Teilnehmer (max 45s) — aber blockiere nicht Greeting wenn Teilnehmer verspätet
    async def _extract_partner_name():
        nonlocal partner_name
        if job_id.startswith("mock-job"):
            return
        try:
            timeout = float(os.getenv("LIVEKIT_WAIT_PARTICIPANT_SECS", "45"))
            participant = await asyncio.wait_for(ctx.wait_for_participant(), timeout=timeout)
            pname = getattr(participant, "name", "") or ""
            if pname.startswith("Partner (") and pname.endswith(")"):
                extracted = pname[9:-1]
                if not extracted.startswith("+") and not extracted.startswith("phone-"):
                    partner_name = extracted
            elif pname and not pname.startswith("phone-") and not pname.startswith("+"):
                partner_name = pname
            if partner_name:
                logger.info("Partner-Name erkannt: %s", partner_name)
        except asyncio.TimeoutError:
            logger.warning("Kein Teilnehmer erkannt (Timeout)")
        except Exception as e:
            logger.warning("Fehler beim Participant-Abruf: %s", e)
    
    # Starte Partner-Name-Erkennung im Hintergrund (maximal 5s, dann trotzdem fortfahren)
    partner_extraction_task = asyncio.create_task(_extract_partner_name())
    try:
        await asyncio.wait_for(partner_extraction_task, timeout=5.0)
    except asyncio.TimeoutError:
        pass

    async def run():
        # Warte auf Session-Bereitschaft (sollte sehr schnell sein)
        await session_start_task
        
        # Sofort Greeting ohne Pause (Partner hat registriert, Gemini bereit)
        name_hint = f" Der Partner heißt {partner_name}." if partner_name else ""
        trigger = f"{_START_TRIGGER_PREFIX} Der Partner hat abgenommen.{name_hint} Begrüße ihn jetzt sofort freundlich und erkläre dein Anliegen."
        logger.info("🎤 Greeting sofort: %s", trigger[:80])
        try:
            session.generate_reply(user_input=trigger)
        except Exception as e:
            logger.error("Gesprächseröffnung fehlgeschlagen: %s", e)

    # Starte Call-Monitoring + Greeting parallel
    await asyncio.gather(run(), end_call_monitor(ctx, _finalize, session))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    agents.cli.run_app(server)
