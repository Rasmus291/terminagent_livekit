"""
Response Handler — Verarbeitet alle eingehenden Gemini Live API Responses.

Diese Klasse kapselt die gesamte Logik für die Verarbeitung von Server-Antworten
während einer Live-Audio-Session. Sie ist zuständig für:

1. USER-TRANSKRIPTION: Empfängt die Echtzeit-Transkription des Gesprächspartners
   und speichert sie mit Timestamp im Session-Transkript.

2. AGENT-TRANSKRIPTION: Sammelt die inkrementellen Text-Fragmente der KI-Antwort
   über einen gesamten Turn hinweg und speichert den vollständigen Text erst,
   wenn der Turn abgeschlossen ist (turn_complete).

3. AUDIO-WIEDERGABE: Leitet die empfangenen Audio-Chunks des Agents an den
   AudioStreamer weiter. Erkennt Unterbrechungen durch den Partner und leert
   dann die Wiedergabe-Queue.

4. LATENZ-MESSUNG: Misst die Zeit zwischen dem letzten gesendeten Audio-Chunk
   und dem ersten empfangenen Audio-Response pro Turn. Berechnet laufend
   den Durchschnitt über alle Turns der Session.

5. TOOL-CALL VERARBEITUNG: Delegiert Function Calls (z.B. schedule_appointment)
   an den Tool Handler und signalisiert, wenn das Gespräch beendet werden soll.
"""

import asyncio
import datetime
import logging
import time

from tool_handler import process_tool_calls

logger = logging.getLogger(__name__)


class ResponseHandler:
    """Verarbeitet alle eingehenden Responses einer Gemini Live Session.
    
    Attribute:
        transcript: Liste aller Gesprächsbeiträge mit Timestamps
        latency_measurements: Liste aller gemessenen Latenzen in ms
        crm_data: Dict mit CRM-Daten (wird vom Tool Handler befüllt)
        current_turn_text: Aktuell laufender Agent-Text (unvollständiger Turn)
    """

    def __init__(self, audio_streamer, transcript, crm_data, latency_measurements):
        self.audio_streamer = audio_streamer
        self.transcript = transcript
        self.crm_data = crm_data
        self.latency_measurements = latency_measurements
        
        # Tracking-State pro Turn
        self.current_turn_text = ""
        self._first_audio_in_turn = True
        
        # Wird von außen (send_audio) aktualisiert
        self.last_audio_sent_time = None

    def _handle_interruption(self, server_content):
        """Erkennt wenn der Partner den Agent unterbricht und leert die Audio-Queue."""
        if getattr(server_content, 'interrupted', False):
            logger.info("Agent unterbrochen.")
            self.audio_streamer.clear_output()

    def _handle_user_transcription(self, server_content):
        """Speichert die Echtzeit-Transkription des Partners ins Transkript."""
        input_t = getattr(server_content, 'input_transcription', None)
        if input_t and getattr(input_t, 'text', None):
            text = input_t.text
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.info(f"User: {text}")
            self.transcript.append(f"**[{ts}] User:** {text}")

    def _handle_agent_transcription(self, server_content):
        """Sammelt inkrementelle Agent-Transkription über den gesamten Turn."""
        output_t = getattr(server_content, 'output_transcription', None)
        if output_t and getattr(output_t, 'text', None):
            self.current_turn_text += output_t.text

    def _handle_audio_and_latency(self, server_content):
        """Spielt Agent-Audio ab und misst die Latenz beim ersten Chunk des Turns."""
        model_turn = getattr(server_content, 'model_turn', None)
        if model_turn is None:
            return
        
        for part in model_turn.parts:
            if getattr(part, 'inline_data', None):
                # Latenz: Zeit vom letzten gesendeten Chunk bis zum ersten empfangenen
                if self._first_audio_in_turn and self.last_audio_sent_time is not None:
                    latency_ms = (time.perf_counter() - self.last_audio_sent_time) * 1000
                    self.latency_measurements.append(latency_ms)
                    avg = sum(self.latency_measurements) / len(self.latency_measurements)
                    logger.info(f"⚡ Latenz: {latency_ms:.0f}ms | Ø {avg:.0f}ms (n={len(self.latency_measurements)})")
                    self._first_audio_in_turn = False
                
                self.audio_streamer.play_output_stream(part.inline_data.data)

    def _handle_turn_complete(self, server_content):
        """Speichert den vollständigen Agent-Text wenn der Turn abgeschlossen ist."""
        if getattr(server_content, 'turn_complete', False):
            if self.current_turn_text.strip():
                ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                logger.info(f"Agent: {self.current_turn_text}")
                self.transcript.append(f"**[{ts}] Agent:** {self.current_turn_text}")
            self.current_turn_text = ""

    async def process_turn(self, session):
        """Verarbeitet einen kompletten Turn (mehrere Responses bis turn_complete).
        
        Returns:
            bool: True wenn das Gespräch beendet werden soll.
        """
        turn = session.receive()
        self.current_turn_text = ""
        self._first_audio_in_turn = True
        self.audio_streamer.new_turn()

        async for response in turn:
            # Server Content: Transkription, Audio, Turn-Status
            sc = response.server_content
            if sc is not None:
                self._handle_interruption(sc)
                self._handle_user_transcription(sc)
                self._handle_agent_transcription(sc)
                self._handle_audio_and_latency(sc)
                self._handle_turn_complete(sc)

            # Tool Calls (z.B. schedule_appointment)
            should_end = await process_tool_calls(
                response, session, self.crm_data, self.audio_streamer
            )
            if should_end:
                return True
        
        return False

    def save_pending_text(self):
        """Sichert unvollständigen Agent-Text bei Abbruch mitten im Turn."""
        if self.current_turn_text.strip():
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.transcript.append(f"**[{ts}] Agent:** {self.current_turn_text}")
