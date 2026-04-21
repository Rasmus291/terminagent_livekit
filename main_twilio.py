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

import io
import os
import asyncio
import datetime
import logging
import time
import wave

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
    UserTurnStoppedMessage,
    AssistantTurnStoppedMessage,
)
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketTransport,
    FastAPIWebsocketParams,
)

from config_pipecat import GEMINI_API_KEY, LLM_SETTINGS, TOOLS
from tool_handler_pipecat import (
    handle_check_availability,
    handle_schedule_appointment,
    handle_end_call,
    crm_data_saved,
    mark_partner_farewell,
    reset_call_state,
)
from reporting_pipecat import save_session_report, generate_analysis

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

app = FastAPI()


@app.post("/twilio/incoming")
async def twilio_incoming(request: Request):
    """Webhook für eingehende/ausgehende Twilio-Anrufe.

    Twilio ruft diese URL auf wenn ein Anruf verbunden wird.
    Antwort: TwiML das Twilio anweist, einen Media Stream WebSocket zu öffnen.
    """
    # Bestimme die WebSocket-URL basierend auf dem Host-Header
    host = request.headers.get("host", f"{HOST}:{PORT}")
    # Verwende wss:// wenn hinter einem Proxy (ngrok, etc.)
    ws_scheme = "wss" if request.headers.get("x-forwarded-proto") == "https" else "ws"
    ws_url = f"{ws_scheme}://{host}/twilio/ws"

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{ws_url}">
            <Parameter name="caller_id" value="{{{{From}}}}" />
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
    caller_id = start_msg.get("start", {}).get("customParameters", {}).get("caller_id", "unbekannt")

    logger.info(f"Twilio Stream gestartet: stream_sid={stream_sid}, call_sid={call_sid}, caller={caller_id}")

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
        ),
    )

    # --- LLM ---
    llm = GeminiLiveLLMService(
        api_key=GEMINI_API_KEY,
        settings=LLM_SETTINGS,
        tools=TOOLS,
    )
    llm.register_function("check_availability", handle_check_availability)
    llm.register_function("schedule_appointment", handle_schedule_appointment)
    llm.register_function("end_call", handle_end_call)

    # --- Context ---
    context = LLMContext(
        [
            {
                "role": "user",
                "content": "Der Partner hat gerade abgenommen. Begrüße ihn jetzt und starte das Gespräch.",
            },
        ],
    )
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context)

    # --- Audio Recording ---
    audiobuffer = AudioBufferProcessor(num_channels=1)

    @audiobuffer.event_handler("on_track_audio_data")
    async def on_track_audio_data(buffer, user_audio, bot_audio, sample_rate, num_channels):
        os.makedirs("sessions", exist_ok=True)
        if len(user_audio) > 0:
            user_path = f"sessions/recording_user_{session_timestamp}.wav"
            with wave.open(user_path, "wb") as wf:
                wf.setsampwidth(2)
                wf.setnchannels(1)
                wf.setframerate(sample_rate)
                wf.writeframes(user_audio)
            logger.info(f"User-Audio gespeichert: {user_path}")
        if len(bot_audio) > 0:
            bot_path = f"sessions/recording_agent_{session_timestamp}.wav"
            with wave.open(bot_path, "wb") as wf:
                wf.setsampwidth(2)
                wf.setnchannels(1)
                wf.setframerate(sample_rate)
                wf.writeframes(bot_audio)
            logger.info(f"Agent-Audio gespeichert: {bot_path}")

    @audiobuffer.event_handler("on_audio_data")
    async def on_audio_data(buffer, audio, sample_rate, num_channels):
        if len(audio) > 0:
            mono_path = f"sessions/recording_{session_timestamp}.wav"
            with wave.open(mono_path, "wb") as wf:
                wf.setsampwidth(2)
                wf.setnchannels(1)
                wf.setframerate(sample_rate)
                wf.writeframes(audio)
            logger.info(f"Mono-Aufnahme gespeichert: {mono_path}")

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
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if message.content:
            logger.info(f"Agent: {message.content}")
            session_transcript.append(f"**[{ts}] Agent:** {message.content}")

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
        # Session Report
        call_duration = time.perf_counter() - session_start_perf
        call_start_str = session_start_time.strftime("%Y-%m-%d %H:%M:%S")

        logger.info("Generiere Analyse...")
        analysis = generate_analysis(session_transcript)

        logger.info("Speichere Session Report...")
        save_session_report(
            session_transcript,
            crm_data=crm_data_saved or None,
            call_duration=call_duration,
            call_start_time=call_start_str,
            analysis=analysis,
        )
        logger.info(f"Call beendet. Dauer: {call_duration:.0f}s, Caller: {caller_id}")


@app.post("/twilio/call")
async def initiate_call(request: Request):
    """REST-Endpoint um einen ausgehenden Anruf zu starten.

    POST /twilio/call
    Body: {"to": "+49170XXXXXXX"}
    """
    try:
        from twilio.rest import Client as TwilioClient
    except ImportError:
        return PlainTextResponse(
            content="twilio Python SDK nicht installiert. pip install twilio",
            status_code=500,
        )

    body = await request.json()
    to_number = body.get("to")
    if not to_number:
        return PlainTextResponse(content="'to' Telefonnummer fehlt", status_code=400)

    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER]):
        return PlainTextResponse(
            content="Twilio Credentials fehlen in .env",
            status_code=500,
        )

    host = request.headers.get("host", f"{HOST}:{PORT}")
    scheme = "https" if request.headers.get("x-forwarded-proto") == "https" else "http"
    webhook_url = f"{scheme}://{host}/twilio/incoming"

    client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    call = client.calls.create(
        to=to_number,
        from_=TWILIO_PHONE_NUMBER,
        url=webhook_url,
        machine_detection="Enable",
        machine_detection_timeout=5,
    )

    logger.info(f"Ausgehender Anruf gestartet: {call.sid} → {to_number}")
    return {"call_sid": call.sid, "status": call.status, "to": to_number}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "terminagent-twilio"}


if __name__ == "__main__":
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY fehlt in .env!")
        exit(1)

    logger.info(f"Starte Twilio Server auf {HOST}:{PORT}")
    logger.info(f"Twilio Webhook: http://{HOST}:{PORT}/twilio/incoming")
    logger.info(f"Outbound Calls: POST http://{HOST}:{PORT}/twilio/call")

    uvicorn.run(app, host=HOST, port=PORT)
