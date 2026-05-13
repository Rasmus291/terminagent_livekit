"""Audio-Aufnahme und Live-Monitor-Streaming für LiveKit Rooms."""

import asyncio
import base64
import datetime
import logging
import os
import struct
import wave

from livekit import rtc

logger = logging.getLogger(__name__)

RECORDING_SAMPLE_RATE = 16000


class RoomAudioRecorder:
    """Zeichnet Audio aus dem LiveKit Room auf (Partner + Agent als Stereo-WAV)
    und streamt es optional an den Live-Monitor."""

    MONITOR_URL = os.getenv("MONITOR_API_URL", "http://localhost:8080")
    MONITOR_CHUNK_FRAMES = 3  # ~30ms Buffer für minimale Latenz

    def __init__(self, sample_rate: int = RECORDING_SAMPLE_RATE):
        self.sample_rate = sample_rate
        self._partner_frames: list[bytes] = []
        self._agent_frames: list[bytes] = []
        self._recording = False
        self._partner_task: asyncio.Task | None = None
        self._agent_task: asyncio.Task | None = None
        self._room: rtc.Room | None = None
        self._http_session = None

    def start(self, room: rtc.Room):
        self._recording = True
        self._room = room
        self._monitor_send_failures = 0

        @room.on("track_subscribed")
        def _on_remote_track(track, publication, participant):
            if track.kind == rtc.TrackKind.KIND_AUDIO and self._partner_task is None:
                logger.info("Partner-Audio-Track abonniert (%s)", participant.identity)
                self._partner_task = asyncio.create_task(
                    self._capture_loop(track, self._partner_frames, "Partner")
                )

        @room.on("local_track_published")
        def _on_local_track(publication, track):
            if track.kind == rtc.TrackKind.KIND_AUDIO and self._agent_task is None:
                logger.info("Agent-Audio-Track veröffentlicht — starte Aufnahme.")
                self._agent_task = asyncio.create_task(
                    self._capture_loop(track, self._agent_frames, "Agent")
                )

        # Bereits abonnierte Remote-Tracks sofort erfassen
        for participant in room.remote_participants.values():
            for pub in participant.track_publications.values():
                track = pub.track
                if track and track.kind == rtc.TrackKind.KIND_AUDIO and self._partner_task is None:
                    logger.info("Bestehender Partner-Audio-Track gefunden (%s)", participant.identity)
                    self._partner_task = asyncio.create_task(
                        self._capture_loop(track, self._partner_frames, "Partner")
                    )

    async def _capture_loop(self, track, frame_list: list[bytes], label: str):
        track_name = "partner" if label == "Partner" else "agent"
        monitor_buf: list[bytes] = []
        try:
            stream = rtc.AudioStream(track, sample_rate=self.sample_rate, num_channels=1)
            async for event in stream:
                if not self._recording:
                    break
                raw = event.frame.data.tobytes()
                frame_list.append(raw)
                monitor_buf.append(raw)
                if len(monitor_buf) >= self.MONITOR_CHUNK_FRAMES:
                    await self._send_to_monitor(track_name, b"".join(monitor_buf))
                    monitor_buf.clear()
        except Exception as e:
            logger.warning("%s-Audio-Capture abgebrochen: %s", label, e)
        finally:
            if monitor_buf:
                await self._send_to_monitor(track_name, b"".join(monitor_buf))
            logger.info("%s-Audio-Capture beendet. Frames: %d", label, len(frame_list))

    async def _send_to_monitor(self, track: str, pcm_data: bytes):
        try:
            if self._http_session is None or self._http_session.closed:
                import aiohttp
                if self._http_session and self._http_session.closed:
                    logger.info("Monitor HTTP-Session war geschlossen — erstelle neue.")
                self._http_session = aiohttp.ClientSession()
            import aiohttp
            b64 = base64.b64encode(pcm_data).decode("ascii")
            async with self._http_session.post(
                f"{self.MONITOR_URL}/monitor/audio",
                json={"track": track, "sample_rate": self.sample_rate, "pcm16_b64": b64},
                timeout=aiohttp.ClientTimeout(total=2),
            ):
                # Erfolg — Reset Failure-Counter
                if self._monitor_send_failures > 0:
                    logger.info("Monitor-Audio wieder erreichbar nach %d Fehlern.", self._monitor_send_failures)
                    self._monitor_send_failures = 0
        except Exception as e:
            self._monitor_send_failures += 1
            # Logge nur die ersten 3 Fehler und dann alle 50
            if self._monitor_send_failures <= 3 or self._monitor_send_failures % 50 == 0:
                logger.warning("Monitor-Audio senden fehlgeschlagen (#%d): %s", self._monitor_send_failures, e)

    def stop(self):
        self._recording = False
        for task in (self._partner_task, self._agent_task):
            if task and not task.done():
                task.cancel()

    async def close(self):
        self.stop()
        if self._http_session:
            await self._http_session.close()
            self._http_session = None

    async def notify_call_start(self, contact_name: str = ""):
        await self._send_call_state({"event": "call-start", "active": True, "contact_name": contact_name})

    async def notify_call_end(self):
        await self._send_call_state({"event": "call-end", "active": False})

    async def _send_call_state(self, state: dict):
        """Sendet Call-State an Monitor mit Retry."""
        for attempt in range(3):
            try:
                if self._http_session is None or self._http_session.closed:
                    import aiohttp
                    self._http_session = aiohttp.ClientSession()
                import aiohttp
                async with self._http_session.post(
                    f"{self.MONITOR_URL}/monitor/call-state",
                    json=state,
                    timeout=aiohttp.ClientTimeout(total=3),
                ) as resp:
                    logger.info("Monitor call-state gesendet: %s -> %s", state.get("event"), resp.status)
                    return
            except Exception as e:
                logger.warning("Monitor call-state Versuch %d fehlgeschlagen: %s", attempt + 1, e)
                if attempt < 2:
                    await asyncio.sleep(1)

    def save(self, directory: str = "sessions", timestamp: str | None = None) -> dict:
        if not self._partner_frames and not self._agent_frames:
            logger.info("Keine Audio-Frames zum Speichern.")
            return {}

        os.makedirs(directory, exist_ok=True)
        timestamp = timestamp or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        partner_data = b"".join(self._partner_frames)
        agent_data = b"".join(self._agent_frames)

        max_len = max(len(partner_data), len(agent_data))
        partner_data = partner_data.ljust(max_len, b"\x00")
        agent_data = agent_data.ljust(max_len, b"\x00")

        num_samples = max_len // 2
        partner_samples = struct.unpack(f"<{num_samples}h", partner_data[:num_samples * 2])
        agent_samples = struct.unpack(f"<{num_samples}h", agent_data[:num_samples * 2])

        stereo = bytearray()
        for p, a in zip(partner_samples, agent_samples):
            stereo.extend(struct.pack("<hh", p, a))

        path = os.path.join(directory, f"recording_{timestamp}.wav")
        with wave.open(path, "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(self.sample_rate)
            wf.writeframes(bytes(stereo))

        duration = num_samples / self.sample_rate
        logger.info("Stereo-Audio gespeichert: %s (%.1fs)", path, duration)
        return {"recording": path, "duration_seconds": round(duration, 1)}
