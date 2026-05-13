"""LiveKit Agent Entry Point — Orchestriert Session-Lifecycle."""

import asyncio
import datetime
import logging
import os
import time

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, RoomInputOptions
from livekit.plugins import google, silero
from google.genai import types as genai_types

from audio_recorder import RoomAudioRecorder
from config import GEMINI_API_KEY, MODEL_ID, SYSTEM_INSTRUCTION
from latency_profiler import LatencyProfiler
from session_manager import (
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

    try:
        await _run_agent(ctx)
    except Exception as e:
        logger.error("Unerwarteter Fehler im Agent: %s", e, exc_info=True)
        # Graceful Cleanup: Room verlassen damit der nächste Worker übernehmen kann
        try:
            await asyncio.sleep(0.3)
            await ctx.room.disconnect()
        except Exception:
            pass


async def _run_agent(ctx: JobContext):

    os.makedirs("sessions", exist_ok=True)
    reset_call_state()
    # Event SOFORT im aktuellen Loop erstellen (Python 3.13 kompatibel).
    # Muss vor participant_disconnected-Handler erstellt werden!
    import tool_handler as _th_init
    _th_init.call_ended = asyncio.Event()

    transcript: list[str] = []
    latencies: list[float] = []
    start_time = datetime.datetime.now()
    start_perf = time.perf_counter()
    ts = start_time.strftime("%Y%m%d_%H%M%S")
    started_event = asyncio.Event()
    recorder = RoomAudioRecorder()
    profiler = LatencyProfiler()

    _done = False

    async def _finalize(reason=""):
        nonlocal _done
        if _done:
            return
        _done = True
        # Latenz-Profil ausgeben
        profiler.print_summary()
        await finalize_session(
            transcript, crm_data_saved or None, recorder,
            start_time, start_perf, ts, latencies,
        )

    ctx.add_shutdown_callback(_finalize)

    await ctx.connect()
    recorder.start(ctx.room)
    logger.info("Room verbunden — starte Session + warte auf Partner parallel.")

    instructions = SYSTEM_INSTRUCTION

    # Session SOFORT erstellen und starten (Gemini aufwärmen) — parallel zum Warten auf Partner
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
                    startOfSpeechSensitivity=genai_types.StartSensitivity.START_SENSITIVITY_HIGH,
                    endOfSpeechSensitivity=genai_types.EndSensitivity.END_SENSITIVITY_LOW,
                    silenceDurationMs=80, prefixPaddingMs=0,
                ),
            ),
        ),
        vad=None,  # Kein lokaler VAD — Gemini's eigenes Endpointing reicht
        turn_handling={
            "turn_detection": "realtime_llm",
            "endpointing": {"mode": "dynamic", "min_delay": 0.0, "max_delay": 0.0},
        },
    )

    # Event-Handler registrieren
    handler = create_conversation_handler(transcript, latencies, started_event, {
        "mark_partner_farewell": mark_partner_farewell,
        "mark_assistant_farewell": mark_assistant_farewell,
    })
    session.on("conversation_item_added", handler)
    register_audio_latency_events(session)
    profiler.register(session)

    def on_close(event):
        import tool_handler as _th
        if _th.call_ended and not _th.call_ended.is_set():
            _th.call_ended.set()
    session.on("close", on_close)

    # Participant-Disconnect erkennen (Partner legt auf)
    @ctx.room.on("participant_disconnected")
    def on_participant_left(participant):
        import tool_handler as _th
        logger.info("Participant disconnected: %s", getattr(participant, 'identity', ''))
        if _th.call_ended and not _th.call_ended.is_set():
            _th.call_ended.set()

    # Session starten (verbindet zu Gemini, Audio-Pipeline aufbauen)
    room_input = RoomInputOptions(close_on_disconnect=False, pre_connect_audio=False, audio_frame_size_ms=10)
    session_start_task = asyncio.create_task(session.start(room=ctx.room, agent=agent, room_input_options=room_input))

    job_id = str(getattr(getattr(ctx, "job", None), "id", ""))
    partner_name = ""

    # Partner-Name + Anrede aus Dispatch-Metadata extrahieren
    dispatch_metadata = getattr(getattr(ctx, "job", None), "metadata", "") or ""
    partner_salutation = ""
    if dispatch_metadata:
        try:
            import json as _json
            meta = _json.loads(dispatch_metadata)
            partner_name = meta.get("name", "")
            partner_salutation = meta.get("salutation", "")
            logger.info("Partner aus Metadata: salutation='%s', name='%s'", partner_salutation, partner_name)
        except (ValueError, TypeError):
            if not dispatch_metadata.startswith("+") and not dispatch_metadata.startswith("phone-"):
                partner_name = dispatch_metadata

    # Gemini-Session bereit machen
    await session_start_task
    logger.info("Gemini-Session bereit.")

    # Fallback: Agent-Track manuell für Recorder finden
    await asyncio.sleep(0.3)
    if recorder._agent_task is None:
        from livekit import rtc
        for pub in ctx.room.local_participant.track_publications.values():
            track = pub.track
            if track and track.kind == rtc.TrackKind.KIND_AUDIO:
                logger.info("Agent-Track manuell für Recorder verbunden (Fallback).")
                recorder._agent_task = asyncio.create_task(
                    recorder._capture_loop(track, recorder._agent_frames, "Agent")
                )
                break

    # Auf Participant warten (Partner nimmt ab)
    if not job_id.startswith("mock-job"):
        timeout = float(os.getenv("LIVEKIT_WAIT_PARTICIPANT_SECS", "90"))
        try:
            participant = await asyncio.wait_for(ctx.wait_for_participant(), timeout=timeout)
            pname = getattr(participant, "name", "") or ""
            logger.info("Participant connected: '%s'", pname)
            if not partner_name:
                if pname.startswith("Partner (") and pname.endswith(")"):
                    extracted = pname[9:-1]
                    if not extracted.startswith("+") and not extracted.startswith("phone-"):
                        partner_name = extracted
                elif pname and not pname.startswith("phone-") and not pname.startswith("+"):
                    partner_name = pname
        except asyncio.TimeoutError:
            logger.warning("Kein Teilnehmer innerhalb von %.0fs.", timeout)

    asyncio.create_task(recorder.notify_call_start(contact_name=partner_name))

    # Prüfen ob Anruf schon beendet
    from tool_handler import call_ended as _ce
    if _ce and _ce.is_set():
        logger.info("Anruf bereits beendet — überspringe.")
        await end_call_monitor(ctx, _finalize, session)
        return

    # Partner hat abgenommen — 3s warten
    logger.info("Partner hat abgenommen — warte 3s bevor Begrüßung gestartet wird.")
    await asyncio.sleep(5)

    # Begrüßung via generate_reply triggern (funktioniert mit gemini-2.5)
    from session_manager import _START_TRIGGER_PREFIX
    if partner_salutation and partner_name:
        name_hint = f" Der Partner heißt {partner_salutation} {partner_name}. Begrüße mit 'Hallo {partner_salutation} {partner_name}, hier ist Anna von LaVita.'"
    elif partner_name:
        name_hint = f" Der Partner heißt {partner_name}. Begrüße mit 'Hallo Herr/Frau {partner_name}, hier ist Anna von LaVita.'"
    else:
        name_hint = ""
    greeting_trigger = f"{_START_TRIGGER_PREFIX} Der Partner hat abgenommen.{name_hint} Begrüße ihn jetzt freundlich und erkläre kurz dein Anliegen."
    try:
        session.generate_reply(user_input=greeting_trigger)
        logger.info("Begrüßungs-Trigger gesendet.")
    except Exception as e:
        logger.error("Begrüßungs-Trigger fehlgeschlagen: %s", e)

    # Gesundheitscheck: Warte max 10s auf erste Agent-Audio
    try:
        await asyncio.wait_for(started_event.wait(), timeout=10.0)
        logger.info("✅ Gesundheitscheck: Agent hat gesprochen.")
    except asyncio.TimeoutError:
        logger.error("❌ FEHLER: Agent hat nach 10s nicht gesprochen!")

    await end_call_monitor(ctx, _finalize, session)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    agents.cli.run_app(server)
