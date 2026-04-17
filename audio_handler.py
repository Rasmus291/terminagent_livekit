import sounddevice as sd
import asyncio
import threading
import queue

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
        
        # Event Loop Referenz für Thread-sicheres Queuing aus dem Audio-Callback
        try:
            self.loop = asyncio.get_event_loop()
        except RuntimeError:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

    def start(self):
        """Initialisiert Audio-Streams und das Callback-System."""
        self.is_running = True
        
        def input_callback(indata, frames, time, status):
            if self.is_running:
                # Sicherer Push der Audio-Daten vom Audio-Thread in die asynchrone Python-Queue
                self.loop.call_soon_threadsafe(self.input_queue.put_nowait, bytes(indata))

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
