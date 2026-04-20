"""
Pipecat-basierter LaVita Terminvereinbarungs-Agent.

Nutzt Gemini Live API als All-in-One (STT + LLM + TTS) über Pipcats
GeminiLiveLLMService mit LocalAudioTransport (Mikrofon/Lautsprecher).

Starten mit: python main_pipecat.py
Bisheriges System weiterhin verfügbar unter: python main.py
"""

import array
import io
import math
import os
import asyncio
import datetime
import logging
import struct
import time
import wave

from pipecat.frames.frames import EndFrame, InputAudioRawFrame, LLMRunFrame
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
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

from config_pipecat import GEMINI_API_KEY, LLM_SETTINGS, TOOLS
from tool_handler_pipecat import handle_schedule_appointment, handle_end_call, crm_data_saved, appointment_done, call_ended
from reporting_pipecat import save_session_report, generate_analysis

# Logging Setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def match_audio_volume(source_audio, target_audio):
    """Passt die Lautstärke von source_audio an das Niveau von target_audio an (RMS-basiert)."""
    src = array.array('h', source_audio)
    tgt = array.array('h', target_audio)

    src_rms = math.sqrt(sum(s * s for s in src) / len(src))
    tgt_rms = math.sqrt(sum(s * s for s in tgt) / len(tgt))

    if src_rms < 1:
        return source_audio

    gain = min(tgt_rms / src_rms, 10.0)
    if 0.9 <= gain <= 1.1:
        return source_audio

    for i in range(len(src)):
        src[i] = max(-32768, min(32767, int(src[i] * gain)))

    return src.tobytes()


class InputAudioGain(FrameProcessor):
    """Verstärkt Mikrofon-Audio um einen konfigurierbaren Faktor."""

    def __init__(self, gain: float = 3.0, **kwargs):
        super().__init__(**kwargs)
        self.gain = gain

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame) and self.gain != 1.0:
            samples = array.array('h', frame.audio)
            for i in range(len(samples)):
                samples[i] = max(-32768, min(32767, int(samples[i] * self.gain)))
            frame = InputAudioRawFrame(
                audio=samples.tobytes(),
                sample_rate=frame.sample_rate,
                num_channels=frame.num_channels,
            )
        await self.push_frame(frame, direction)


async def main():
    if not GEMINI_API_KEY:
        logger.error("API-Key fehlt! Bitte GEMINI_API_KEY in der .env setzen.")
        return

    os.makedirs("sessions", exist_ok=True)

    # Session-Tracking
    session_transcript = []
    session_start_time = datetime.datetime.now()
    session_start_perf = time.perf_counter()

    # --- Transport: Lokales Audio (Mikrofon + Lautsprecher) ---
    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=24000,
        )
    )

    # --- LLM: Gemini Live (All-in-One: STT + LLM + TTS) ---
    llm = GeminiLiveLLMService(
        api_key=GEMINI_API_KEY,
        settings=LLM_SETTINGS,
        tools=TOOLS,
    )

    # Tools registrieren
    llm.register_function("schedule_appointment", handle_schedule_appointment)
    llm.register_function("end_call", handle_end_call)

    # --- Mikrofon-Verstärkung (3x Gain für bessere Spracherkennung) ---
    input_gain = InputAudioGain(gain=3.0)

    # --- Context + Aggregators ---
    # Initialer Context: Begrüßungs-Trigger (wie in main.py)
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
    session_timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    @audiobuffer.event_handler("on_audio_data")
    async def on_audio_data(buffer, audio, sample_rate, num_channels):
        """Speichert das kombinierte Mono-Audio (User + Agent gemischt) als WAV."""
        if len(audio) > 0:
            mono_path = f"sessions/recording_{session_timestamp}.wav"
            with wave.open(mono_path, "wb") as wf:
                wf.setsampwidth(2)
                wf.setnchannels(1)
                wf.setframerate(sample_rate)
                wf.writeframes(audio)
            logger.info(f"Mono-Aufnahme gespeichert: {mono_path}")

    # --- Transkription via Event Handler ---
    @user_aggregator.event_handler("on_user_turn_stopped")
    async def on_user_turn(aggregator, strategy, message: UserTurnStoppedMessage):
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        text = message.content
        if text:
            logger.info(f"User: {text}")
            session_transcript.append(f"**[{ts}] User:** {text}")

    @assistant_aggregator.event_handler("on_assistant_turn_stopped")
    async def on_assistant_turn(aggregator, message: AssistantTurnStoppedMessage):
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        text = message.content
        if text:
            logger.info(f"Agent: {text}")
            session_transcript.append(f"**[{ts}] Agent:** {text}")

    # --- Pipeline ---
    pipeline = Pipeline(
        [
            transport.input(),
            input_gain,
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

    # --- Runner ---
    runner = PipelineRunner(handle_sigint=True)

    logger.info("Starte Pipecat Pipeline mit Gemini Live...")
    logger.info("Du kannst jetzt sprechen. Beende mit Ctrl+C.")

    # End-Call Monitor: Beendet Pipeline wenn end_call Tool ausgelöst wird
    async def end_call_monitor():
        await call_ended.wait()
        logger.info("End-call Signal empfangen. Warte auf letzte Audio-Wiedergabe...")
        await asyncio.sleep(3)
        await task.queue_frames([EndFrame()])

    try:
        # Audio-Recording starten
        await audiobuffer.start_recording()

        # Monitor starten der auf end_call wartet
        monitor = asyncio.create_task(end_call_monitor())

        # Conversation starten (Gemini beginnt sofort mit Begrüßung)
        await task.queue_frames([LLMRunFrame()])
        await runner.run(task)
    except KeyboardInterrupt:
        logger.info("Beendet durch Benutzer (Ctrl+C).")
    finally:
        # Monitor-Task aufräumen
        if 'monitor' in locals() and not monitor.done():
            monitor.cancel()

        # Session Report generieren
        call_duration = time.perf_counter() - session_start_perf
        call_start_str = session_start_time.strftime("%Y-%m-%d %H:%M:%S")

        logger.info("Generiere Analyse + Sentiment...")
        try:
            analysis = await asyncio.to_thread(generate_analysis, session_transcript)
        except Exception as e:
            logger.error(f"Analyse fehlgeschlagen: {e}", exc_info=True)
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
        )

        logger.info("Session abgeschlossen.")


if __name__ == "__main__":
    asyncio.run(main())
