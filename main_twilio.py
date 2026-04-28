"""
Twilio-Server für den LaVita Pipecat Agent.

Dieser Server empfängt eingehende Twilio-Anrufe und verbindet sie mit der
Pipecat Pipeline (Gemini Live). Für ausgehende Anrufe wird die Twilio REST API
genutzt.

Voraussetzungen:
  1. Twilio Account + Telefonnummer
  2. ngrok (für lokales Testen) oder ein öffentlicher Server
  3. .env mit: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER, GEMINI_API_KEY

Starten:
  python main_twilio.py

Dann ngrok starten:
  ngrok http 8765

Twilio Webhook URL setzen auf:
  https://<ngrok-url>/twilio/incoming
"""

import os
import asyncio
import datetime
import logging
import time
import wave
from copy import deepcopy
from urllib.parse import urlencode, parse_qs

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import PlainTextResponse

from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
    UserTurnStoppedMessage,
    AssistantTurnStoppedMessage,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.turns.user_start import VADUserTurnStartStrategy, TranscriptionUserTurnStartStrategy
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketTransport,
    FastAPIWebsocketParams,
)

from config_pipecat import GEMINI_API_KEY, LLM_SETTINGS, TOOLS
from contacts_excel import find_contact, get_contacts_excel_path, load_contacts, normalize_phone
import email_service
from tool_handler_pipecat import (
    handle_check_availability,
    handle_schedule_appointment,
    handle_end_call,
    call_ended,
    crm_data_saved,
    mark_partner_farewell,
    mark_assistant_farewell,
    reset_call_state,
)
from reporting_pipecat import save_session_report, generate_analysis, build_learning_brief

load_dotenv()

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Twilio Credentials
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")

# Server Config
HOST = os.getenv("SERVER_HOST", "0.0.0.0")
PORT = int(os.getenv("SERVER_PORT", "8765"))
MAILBOX_MESSAGE = os.getenv(
    "TWILIO_MAILBOX_MESSAGE",
    "Hallo, hier ist Anna von LaVita. Ich rufe an wegen eines kurzen Termins zur Abstimmung der Zusammenarbeit. "
    "Bitte rufen Sie uns kurz zurück. Vielen Dank und auf Wiederhören.",
)

# Öffentliche URL (wird durch ngrok gesetzt)
PUBLIC_URL = None

app = FastAPI()


@app.post("/twilio/incoming")
async def twilio_incoming(request: Request):
    """Webhook für eingehende/ausgehende Twilio-Anrufe.

    Twilio ruft diese URL auf wenn ein Anruf verbunden wird.
    Antwort: TwiML das Twilio anweist, einen Media Stream WebSocket zu öffnen.
    """
    body_bytes = await request.body()
    form_data = parse_qs(body_bytes.decode("utf-8", errors="ignore")) if body_bytes else {}
    answered_by = str((form_data.get("AnsweredBy") or [""])[0]).strip().lower()

    # Twilio AMD: bei Mailbox/Fax eine kurze Ansage sprechen und auflegen
    machine_answer = (
        answered_by.startswith("machine")
        or answered_by == "fax"
        or answered_by == "unknown"
    )
    if machine_answer:
        logger.info("Mailbox/Fax erkannt (AnsweredBy=%s). Starte Mailbox-Ansage.", answered_by or "-")
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Pause length="1"/>
    <Say voice="alice" language="de-DE">{MAILBOX_MESSAGE}</Say>
    <Hangup/>
</Response>"""
        return PlainTextResponse(content=twiml, media_type="text/xml")

    # Bestimme die WebSocket-URL basierend auf dem Host-Header
    host = request.headers.get("host", f"{HOST}:{PORT}")
    # Verwende wss:// wenn hinter einem Proxy (ngrok, etc.)
    ws_scheme = "wss" if request.headers.get("x-forwarded-proto") == "https" else "ws"
    ws_url = f"{ws_scheme}://{host}/twilio/ws"
    contact_name = (request.query_params.get("contact_name") or "").strip()
    contact_first_name = (request.query_params.get("contact_first_name") or "").strip()
    contact_company = (request.query_params.get("contact_company") or "").strip()
    contact_notes = (request.query_params.get("contact_notes") or "").strip()
    contact_id = (request.query_params.get("contact_id") or "").strip()

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{ws_url}">
            <Parameter name="caller_id" value="{{{{From}}}}" />
            <Parameter name="contact_name" value="{contact_name}" />
            <Parameter name="contact_first_name" value="{contact_first_name}" />
            <Parameter name="contact_company" value="{contact_company}" />
            <Parameter name="contact_notes" value="{contact_notes}" />
            <Parameter name="contact_id" value="{contact_id}" />
        </Stream>
    </Connect>
</Response>"""

    return PlainTextResponse(content=twiml, media_type="text/xml")


@app.websocket("/twilio/ws")
async def twilio_websocket(websocket: WebSocket):
    """WebSocket-Endpoint für Twilio Media Streams.

    Hier wird die Pipecat Pipeline für jeden Anruf instanziiert.
    """
    await websocket.accept()
    reset_call_state()

    # Erste Nachricht von Twilio enthält Stream-Metadaten
    initial_msg = await websocket.receive_json()

    if initial_msg.get("event") != "connected":
        logger.warning(f"Unerwartete erste Nachricht: {initial_msg}")

    # Zweite Nachricht: "start" Event mit stream_sid und call_sid
    start_msg = await websocket.receive_json()
    stream_sid = start_msg.get("streamSid", "")
    call_sid = start_msg.get("start", {}).get("callSid", "")
    custom_parameters = start_msg.get("start", {}).get("customParameters", {})
    caller_id = custom_parameters.get("caller_id", "unbekannt")
    contact_name = custom_parameters.get("contact_name", "")
    contact_first_name = custom_parameters.get("contact_first_name", "")
    contact_company = custom_parameters.get("contact_company", "")
    contact_notes = custom_parameters.get("contact_notes", "")
    contact_id = custom_parameters.get("contact_id", "")

    logger.info(
        f"Twilio Stream gestartet: stream_sid={stream_sid}, call_sid={call_sid}, caller={caller_id}, contact={contact_name or '-'}, contact_id={contact_id or '-'}"
    )

    # Session-Tracking
    session_transcript = []
    session_start_time = datetime.datetime.now()
    session_start_perf = time.perf_counter()
    session_timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    # --- Twilio Serializer ---
    serializer = TwilioFrameSerializer(
        stream_sid=stream_sid,
        call_sid=call_sid,
        account_sid=TWILIO_ACCOUNT_SID,
        auth_token=TWILIO_AUTH_TOKEN,
        params=TwilioFrameSerializer.InputParams(
            sample_rate=16000,
            auto_hang_up=True,
        ),
    )

    # --- Transport ---
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=24000,
            serializer=serializer,
            vad_enabled=True,
            vad_analyzer=SileroVADAnalyzer(
                sample_rate=16000,
            ),
        ),
    )

    # --- LLM ---
    runtime_settings = deepcopy(LLM_SETTINGS)
    learning_brief = build_learning_brief(max_sessions=20)
    if learning_brief:
        runtime_settings.system_instruction = (
            f"{runtime_settings.system_instruction}\n\n{learning_brief}"
        )
        logger.info("Lernkontext aus vergangenen Sessions geladen.")

    if contact_name:
        personalized_context = [
            "KONTAKTKONTEXT FÜR DIESEN ANRUF:",
            f"- Der angerufene Partner heißt: {contact_name}",
        ]
        if contact_first_name:
            personalized_context.append(f"- Vorname: {contact_first_name}")
        if contact_company:
            personalized_context.append(f"- Firma: {contact_company}")
        if contact_notes:
            personalized_context.append(f"- Notizen: {contact_notes}")
        personalized_context.append(
            "- Begrüße diese Person direkt mit Namen. Frage nicht nach dem Namen, sofern der Partner nichts anderes sagt."
        )
        runtime_settings.system_instruction = (
            f"{runtime_settings.system_instruction}\n\n" + "\n".join(personalized_context)
        )

    llm = GeminiLiveLLMService(
        api_key=GEMINI_API_KEY,
        settings=runtime_settings,
        tools=TOOLS,
    )
    llm.register_function("check_availability", handle_check_availability)
    llm.register_function("schedule_appointment", handle_schedule_appointment)
    llm.register_function("end_call", handle_end_call)

    # --- Context ---
    initial_prompt = "Der Partner hat gerade abgenommen. Begrüße ihn jetzt und starte das Gespräch."
    if contact_name:
        initial_prompt = (
            f"Der Partner hat gerade abgenommen. Sein Name ist {contact_name}. "
            "Begrüße ihn direkt mit Namen und starte das Gespräch."
        )

    context = LLMContext(
        [
            {
                "role": "user",
                "content": initial_prompt,
            },
        ],
    )
    user_turn_stop_speech_timeout = float(os.getenv("USER_TURN_SPEECH_TIMEOUT", "0.12"))
    user_turn_stop_timeout = float(os.getenv("USER_TURN_STOP_TIMEOUT", "0.35"))
    user_turn_strategies = UserTurnStrategies(
        start=[VADUserTurnStartStrategy(), TranscriptionUserTurnStartStrategy()],
        stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=user_turn_stop_speech_timeout)],
    )
    user_params = LLMUserAggregatorParams(
        user_turn_strategies=user_turn_strategies,
        user_turn_stop_timeout=user_turn_stop_timeout,
    )
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context, user_params=user_params)

    # --- Audio Recording ---
    audiobuffer = AudioBufferProcessor(sample_rate=16000, num_channels=1)
    recording_user_audio = bytearray()
    recording_agent_audio = bytearray()
    recording_mono_audio = bytearray()
    recording_sample_rate = 16000

    def write_wav(path, audio_bytes, sample_rate, num_channels=1):
        if not audio_bytes:
            return
        os.makedirs("sessions", exist_ok=True)
        with wave.open(path, "wb") as wf:
            wf.setsampwidth(2)
            wf.setnchannels(num_channels)
            wf.setframerate(sample_rate)
            wf.writeframes(audio_bytes)

    @audiobuffer.event_handler("on_track_audio_data")
    async def on_track_audio_data(buffer, user_audio, bot_audio, sample_rate, num_channels):
        nonlocal recording_sample_rate
        recording_sample_rate = sample_rate or recording_sample_rate
        if len(user_audio) > 0:
            recording_user_audio.extend(user_audio)
        if len(bot_audio) > 0:
            recording_agent_audio.extend(bot_audio)

    @audiobuffer.event_handler("on_audio_data")
    async def on_audio_data(buffer, audio, sample_rate, num_channels):
        nonlocal recording_sample_rate
        recording_sample_rate = sample_rate or recording_sample_rate
        if len(audio) > 0:
            recording_mono_audio.extend(audio)

    # --- Transkription ---
    @user_aggregator.event_handler("on_user_turn_stopped")
    async def on_user_turn(aggregator, strategy, message: UserTurnStoppedMessage):
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if message.content:
            logger.info(f"User: {message.content}")
            session_transcript.append(f"**[{ts}] User:** {message.content}")
            mark_partner_farewell(message.content)

    @assistant_aggregator.event_handler("on_assistant_turn_stopped")
    async def on_assistant_turn(aggregator, message: AssistantTurnStoppedMessage):
        if call_ended.is_set():
            logger.debug("Später Assistant-Output nach end_call ignoriert.")
            return
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if message.content:
            logger.info(f"Agent: {message.content}")
            session_transcript.append(f"**[{ts}] Agent:** {message.content}")
            mark_assistant_farewell(message.content)

    # --- Pipeline ---
    pipeline = Pipeline(
        [
            transport.input(),
            user_aggregator,
            llm,
            transport.output(),
            audiobuffer,
            assistant_aggregator,
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    # --- Event Handler ---
    @transport.event_handler("on_client_connected")
    async def on_connected(transport, client):
        logger.info(f"Twilio Client verbunden (Caller: {caller_id})")
        await audiobuffer.start_recording()
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(transport, client):
        logger.info("Twilio Client getrennt")
        await task.cancel()

    # --- Run ---
    runner = PipelineRunner(handle_sigint=False)

    try:
        await runner.run(task)
    finally:
        try:
            await audiobuffer.stop_recording()
        except Exception as e:
            logger.warning("Audio-Aufnahme konnte nicht sauber beendet werden: %s", e)

        mono_path = f"sessions/recording_{session_timestamp}.wav"
        user_path = f"sessions/recording_user_{session_timestamp}.wav"
        agent_path = f"sessions/recording_agent_{session_timestamp}.wav"

        try:
            write_wav(mono_path, recording_mono_audio, recording_sample_rate, num_channels=1)
            write_wav(user_path, recording_user_audio, recording_sample_rate, num_channels=1)
            write_wav(agent_path, recording_agent_audio, recording_sample_rate, num_channels=1)
        except Exception as e:
            logger.warning("Audio-Dateien konnten nicht gespeichert werden: %s", e)

        if recording_mono_audio:
            logger.info("Mono-Aufnahme gespeichert: %s", mono_path)
        if recording_user_audio:
            logger.info("User-Audio gespeichert: %s", user_path)
        if recording_agent_audio:
            logger.info("Agent-Audio gespeichert: %s", agent_path)

        # Session Report
        call_duration = time.perf_counter() - session_start_perf
        call_start_str = session_start_time.strftime("%Y-%m-%d %H:%M:%S")

        logger.info("Generiere Analyse...")
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
        try:
            save_session_report(
                session_transcript,
                crm_data=crm_data_saved or None,
                call_duration=call_duration,
                call_start_time=call_start_str,
                analysis=analysis,
                timestamp=session_timestamp,
            )
        except Exception as e:
            logger.error("Session Report konnte nicht gespeichert werden: %s", e)

        try:
            email_service.send_call_result_summary(
                call_start_time=call_start_str,
                call_duration_seconds=call_duration,
                crm_data=crm_data_saved or None,
                analysis=analysis,
                transcript=session_transcript,
            )
        except Exception as e:
            logger.warning("Ergebnis-Mail Versand fehlgeschlagen: %s", e)

        logger.info(f"Call beendet. Dauer: {call_duration:.0f}s, Caller: {caller_id}")


@app.post("/twilio/call")
async def initiate_call(request: Request):
    """REST-Endpoint um einen ausgehenden Anruf zu starten.

    POST /twilio/call
    Body: {"to": "+49170XXXXXXX"} oder {"contact_id": "1"}
    """
    try:
        from twilio.rest import Client as TwilioClient
    except ImportError:
        return PlainTextResponse(
            content="twilio Python SDK nicht installiert. pip install twilio",
            status_code=500,
        )

    body = await request.json()
    contact_id = body.get("contact_id")
    to_number = body.get("to")
    contact = None

    if contact_id:
        try:
            contact = find_contact(contact_id=str(contact_id))
        except Exception as exc:
            return PlainTextResponse(content=f"Kontakte konnten nicht geladen werden: {exc}", status_code=500)
        if not contact:
            return PlainTextResponse(content=f"Kontakt mit ID {contact_id} nicht gefunden", status_code=404)
        if not to_number:
            to_number = contact.get("phone", "")

    if to_number and not contact:
        try:
            contact = find_contact(phone=to_number)
        except Exception:
            contact = None

    if not to_number:
        return PlainTextResponse(content="'to' Telefonnummer oder 'contact_id' fehlt", status_code=400)

    to_number = normalize_phone(to_number)
    if not to_number:
        return PlainTextResponse(content="Telefonnummer konnte nicht normalisiert werden", status_code=400)

    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER]):
        return PlainTextResponse(
            content="Twilio Credentials fehlen in .env",
            status_code=500,
        )

    # Öffentliche URL für WebSocket-Stream
    if PUBLIC_URL:
        base_url = PUBLIC_URL
    else:
        host = request.headers.get("host", f"{HOST}:{PORT}")
        scheme = "https" if request.headers.get("x-forwarded-proto") == "https" else "http"
        base_url = f"{scheme}://{host}"

    # wss:// für HTTPS, ws:// für HTTP
    ws_base = base_url.replace("https://", "wss://").replace("http://", "ws://")
    ws_url = f"{ws_base}/twilio/ws"

    # Kontaktdaten als Stream-Parameter übergeben
    contact_name = contact.get("name", "") if contact else ""
    contact_first_name = contact.get("first_name", "") if contact else ""
    contact_company = contact.get("company", "") if contact else ""
    contact_notes = contact.get("notes", "") if contact else ""
    contact_id_str = str(contact.get("contact_id", "")) if contact else ""

    # TwiML direkt zusammenbauen (kein Webhook-Roundtrip nötig)
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{ws_url}">
            <Parameter name="contact_name" value="{contact_name}" />
            <Parameter name="contact_first_name" value="{contact_first_name}" />
            <Parameter name="contact_company" value="{contact_company}" />
            <Parameter name="contact_notes" value="{contact_notes}" />
            <Parameter name="contact_id" value="{contact_id_str}" />
        </Stream>
    </Connect>
</Response>"""

    client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    call = client.calls.create(
        to=to_number,
        from_=TWILIO_PHONE_NUMBER,
        twiml=twiml,
        machine_detection="Enable",
        machine_detection_timeout=3,
        async_amd=True,
    )

    logger.info(
        f"Ausgehender Anruf gestartet: {call.sid} → {to_number} ({contact.get('name', 'ohne Kontaktname') if contact else 'ohne Kontaktname'})"
    )
    return {
        "call_sid": call.sid,
        "status": call.status,
        "to": to_number,
        "contact": contact,
    }


@app.get("/twilio/contacts")
async def list_contacts():
    try:
        contacts = load_contacts()
    except Exception as exc:
        return PlainTextResponse(content=f"Kontakte konnten nicht geladen werden: {exc}", status_code=500)

    return {
        "excel_path": get_contacts_excel_path(),
        "count": len(contacts),
        "contacts": contacts,
    }


@app.get("/health")
async def health():
    return {"status": "ok", "service": "terminagent-twilio"}


if __name__ == "__main__":
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY fehlt in .env!")
        exit(1)

    # Öffentliche URL: aus .env oder automatisch via pyngrok
    PUBLIC_URL = os.getenv("PUBLIC_URL")

    if not PUBLIC_URL:
        try:
            from pyngrok import conf as ngrok_conf, ngrok as pyngrok_client

            authtoken = os.getenv("NGROK_AUTHTOKEN")
            if authtoken:
                ngrok_conf.get_default().auth_token = authtoken

            ngrok_conf.get_default().ngrok_version = "v3"
            pyngrok_client.kill()

            tunnel = pyngrok_client.connect(PORT, "http", schemes=["https"])
            PUBLIC_URL = tunnel.public_url
            logger.info(f"ngrok Tunnel aktiv: {PUBLIC_URL}")
        except ImportError:
            logger.warning("pyngrok nicht installiert. Setze PUBLIC_URL in .env oder nutze ngrok manuell.")
        except Exception as e:
            logger.warning(f"ngrok Tunnel konnte nicht gestartet werden: {e}")
            logger.warning("Setze PUBLIC_URL in .env auf deine öffentliche URL.")

    logger.info(f"Starte Twilio Server auf {HOST}:{PORT}")
    if PUBLIC_URL:
        logger.info(f"Twilio Webhook: {PUBLIC_URL}/twilio/incoming")
    else:
        logger.info(f"Twilio Webhook: http://{HOST}:{PORT}/twilio/incoming")
    logger.info(f"Outbound Calls: POST http://{HOST}:{PORT}/twilio/call")

    uvicorn.run(app, host=HOST, port=PORT)
