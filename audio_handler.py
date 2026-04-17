import sounddevice as sd
import asyncio
import threading
import queue
import wave
import struct
import time
import os

class AudioStreamer:
    """Modular class for asynchronous PCM audio streaming (16-bit PCM).
    Switched to sounddevice to improve Mac compatibility without Homebrew/C-Compilers.
    Utilizes a dedicated playback thread to prevent blocking the asyncio event loop.
    """
    
    def __init__(self, input_rate=16000, output_rate=24000, channels=1, chunk_size=512):
        self.input_rate = input_rate
        self.output_rate = output_rate
        self.channels = channels
        self.chunk_size = chunk_size
        
        self.in_stream = None
        self.out_stream = None
        
        self.input_queue = asyncio.Queue()
        self.output_queue = queue.Queue()
        self.is_running = False
        self.playback_thread = None
        
        # Audio-Aufzeichnung: Input fortlaufend, Output mit Zeitstempel für Synchronisation
        self.recording_input = bytearray()                # Partner/Mikrofon (16kHz) — fortlaufend
        self.recording_output_chunks = []                  # Agent (24kHz) — [(zeitpunkt, bytes), ...]
        self._recording_start_time = None                  # Wird beim Start gesetzt
        
        # Event Loop Referenz für Thread-sicheres Queuing aus dem Audio-Callback
        try:
            self.loop = asyncio.get_event_loop()
        except RuntimeError:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

    def start(self):
        """Initialisiert Audio-Streams und das Callback-System."""
        self.is_running = True
        self._recording_start_time = time.perf_counter()
        
        def input_callback(indata, frames, time, status):
            if self.is_running:
                raw = bytes(indata)
                self.recording_input.extend(raw)
                self.loop.call_soon_threadsafe(self.input_queue.put_nowait, raw)

        # RawInputStream nutzt direkt int16 bytes (16-bit PCM little endian)
        self.in_stream = sd.RawInputStream(
            samplerate=self.input_rate,
            channels=self.channels,
            dtype='int16',
            blocksize=self.chunk_size,
            latency='low',
            callback=input_callback
        )
        
        self.out_stream = sd.RawOutputStream(
            samplerate=self.output_rate,
            channels=self.channels,
            dtype='int16',
            blocksize=1024,
            latency='low'
        )

        self.in_stream.start()
        self.out_stream.start()
        
        # Dedizierter Hintergrund-Thread für die Audio-Wiedergabe, 
        # damit die out_stream.write Methode das Event-Loop nicht für Websocket Pings blockiert.
        self.playback_thread = threading.Thread(target=self._playback_loop)
        self.playback_thread.daemon = True
        self.playback_thread.start()

    def _playback_loop(self):
        """Dauerhafte Schleife im Hintergrund-Thread, die Audio wegschreibt."""
        while self.is_running:
            try:
                # Timeout ermöglicht sanftes Beenden bei stop()
                chunk = self.output_queue.get(timeout=0.1)
                if self.is_running and self.out_stream:
                    try:
                        self.out_stream.write(chunk)
                    except Exception:
                        pass
            except queue.Empty:
                pass

    async def get_input_stream(self):
        """Asynchroner Generator, der Mikrofon-Daten liest und yieldet."""
        if not self.is_running:
            self.start()
            
        buffer = bytearray()
        # Sende 100ms Chunks für niedrigere Latenz
        # 16000 Hz * 1 channel * 2 bytes = 32000 bytes/sec -> 3200 bytes = 100ms
        target_bytes = 3200
        
        while self.is_running:
            try:
                chunk = await self.input_queue.get()
                buffer.extend(chunk)
                while len(buffer) >= target_bytes:
                    yield bytes(buffer[:target_bytes])
                    buffer = buffer[target_bytes:]
            except Exception:
                break

    def play_output_stream(self, chunk: bytes):
        """Fügt empfangene 24kHz Audio-Chunks vom Modell in die Queue ein."""
        if self.is_running:
            # Zeitstempel relativ zum Aufnahmestart speichern für spätere Synchronisation
            elapsed = time.perf_counter() - self._recording_start_time
            self.recording_output_chunks.append((elapsed, chunk))
            self.output_queue.put(chunk)

    def clear_output(self):
        """Leert die Wiedergabe-Warteschlange (z.B. wenn der Agent unterbrochen wird)."""
        while not self.output_queue.empty():
            try:
                self.output_queue.get_nowait()
            except queue.Empty:
                break

    def stop(self):
        """Gibt die Hardware-Ressourcen sauber und vollständig frei."""
        self.is_running = False
        
        if self.playback_thread:
            self.playback_thread.join(timeout=1.0)
        
        if self.in_stream:
            self.in_stream.stop()
            self.in_stream.close()
            self.in_stream = None
            
        if self.out_stream:
            self.out_stream.stop()
            self.out_stream.close()
            self.out_stream = None

    def save_recording(self, directory="sessions", timestamp=None):
        """Speichert eine synchronisierte Stereo-WAV (Partner links, Agent rechts)."""
        os.makedirs(directory, exist_ok=True)
        if not timestamp:
            from datetime import datetime
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        if not self.recording_input and not self.recording_output_chunks:
            return {}
        
        # Partner-Track: Fortlaufend aufgenommen, direkt nutzbar
        partner = self.recording_input
        partner_duration = len(partner) / (self.input_rate * 2)  # Sekunden
        
        # Agent-Track: Zeitgestempelte Chunks auf eine durchgehende Timeline legen
        # 1. Agent-Audio von 24kHz auf 16kHz resampling
        # 2. An der richtigen Zeitposition einfügen, Lücken = Stille
        total_samples_16k = len(partner) // 2  # Gleiche Länge wie Partner
        agent_track = bytearray(total_samples_16k * 2)  # Initialisiert mit Stille (Nullbytes)
        
        for elapsed_time, chunk_data in self.recording_output_chunks:
            # Position im 16kHz-Track berechnen
            sample_pos = int(elapsed_time * self.input_rate)
            byte_pos = sample_pos * 2  # 16-bit = 2 bytes pro Sample
            
            # Chunk von 24kHz auf 16kHz resampling
            chunk_16k = self._resample(chunk_data, self.output_rate, self.input_rate)
            
            # In den Track einfügen (ohne über das Ende hinauszuschreiben)
            end_pos = min(byte_pos + len(chunk_16k), len(agent_track))
            available = end_pos - byte_pos
            if available > 0 and byte_pos >= 0:
                agent_track[byte_pos:end_pos] = chunk_16k[:available]
        
        # Stereo interleaven: [Partner_sample, Agent_sample, ...]
        partner_samples = struct.unpack(f'<{len(partner)//2}h', partner)
        agent_samples = struct.unpack(f'<{len(agent_track)//2}h', agent_track)
        
        stereo = bytearray()
        for p, a in zip(partner_samples, agent_samples):
            stereo.extend(struct.pack('<hh', p, a))
        
        path = os.path.join(directory, f"recording_{timestamp}.wav")
        with wave.open(path, "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(self.input_rate)
            wf.writeframes(bytes(stereo))
        
        return {"recording": path}
        
        return {"recording": path}

    @staticmethod
    def _resample(data, from_rate, to_rate):
        """Einfaches Resampling per Sample-Auswahl (reines Python, keine Dependencies)."""
        if not data or from_rate == to_rate:
            return data
        samples = struct.unpack(f'<{len(data)//2}h', data)
        ratio = from_rate / to_rate
        new_count = int(len(samples) / ratio)
        resampled = [samples[min(int(i * ratio), len(samples) - 1)] for i in range(new_count)]
        return struct.pack(f'<{len(resampled)}h', *resampled)
