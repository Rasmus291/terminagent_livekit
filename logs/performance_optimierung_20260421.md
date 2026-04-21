# Performance-Optimierung — 21.04.2026

## Problem
Agent brauchte zu lange zum Laden und Verarbeiten von Informationen.

## Durchgeführte Änderungen

### 1. Audio-Chunk-Größe halbiert (`audio_handler.py`)
- **Vorher:** 100ms Chunks (3200 Bytes) bevor Daten an API gesendet werden
- **Nachher:** 50ms Chunks (1600 Bytes)
- **Warum:** Jeder Audio-Chunk muss vollständig gesammelt werden, bevor er gesendet wird. 100ms bedeutete mindestens 100ms Input-Latenz bei jeder Sprachäußerung. Mit 50ms reagiert der Agent doppelt so schnell auf Spracheingabe.

### 2. Blockierendes `time.sleep()` durch `asyncio.sleep()` ersetzt (`reporting.py`)
- **Vorher:** `time.sleep(5 * (attempt + 1))` — blockiert den gesamten asyncio Event Loop
- **Nachher:** `await asyncio.sleep(2 * (attempt + 1))` — non-blocking, Retry-Wartezeit zusätzlich reduziert
- **Warum:** `time.sleep()` in async Code friert den gesamten Event Loop ein — keine WebSocket-Pings, kein Audio, nichts. Funktion wurde zu `async def` konvertiert.

### 3. Audio-Hardware vor API-Connect gestartet (`main.py`)
- **Vorher:** Audio startet NACH dem API-Verbindungsaufbau (sequenziell)
- **Nachher:** Audio startet VOR dem `client.aio.live.connect()` (parallel)
- **Warum:** Sounddevice-Initialisierung (Hardware-Zugriff, Stream-Setup) dauert 100-300ms. Wenn das parallel zum API-Handshake läuft, spart man diese Zeit komplett.

### 4. Greeting-Delay von 500ms auf 150ms reduziert (`main.py`)
- **Vorher:** `await asyncio.sleep(0.5)` vor dem Begrüßungs-Trigger
- **Nachher:** `await asyncio.sleep(0.15)`
- **Warum:** 500ms war ein willkürlicher Sicherheits-Buffer. 150ms reicht, damit die Audio-Streams stabil laufen, bevor die erste Antwort kommt. Spart 350ms bei Session-Start.

### 5. System-Instruktion um ~70% gekürzt (`config.py`)
- **Vorher:** ~150 Zeilen mit vielen Wiederholungen und Formatierungs-Leerzeichen
- **Nachher:** ~20 Zeilen, gleicher Inhalt, kompakt formuliert
- **Warum:** Die gesamte System-Instruktion wird beim Verbindungsaufbau an die Gemini Live API gesendet und dort verarbeitet. Weniger Tokens = schnelleres Parsing und schnellerer Session-Start. Der inhaltliche Gehalt ist identisch — nur Redundanzen und Formatierung wurden entfernt.

## Geschätzte Verbesserung
- **Input-Latenz:** ~50ms schneller pro Äußerung (Chunk-Größe)
- **Session-Start:** ~500-800ms schneller (Audio parallel + Greeting-Delay + kürzerer Prompt)
- **Retry-Verhalten:** Event Loop wird nicht mehr blockiert bei API-Fehlern

---

### 6. Audio Pre-Buffering gegen Stotterer (`audio_handler.py`, `response_handler.py`)
- **Vorher:** Jeder eingehende Audio-Chunk wird sofort an die Soundkarte geschrieben. Bei `latency='low'` ist der PortAudio-Buffer winzig → wenn der nächste Chunk vom Netzwerk ein paar ms zu spät kommt, entsteht ein Underrun (Stotterer/Verschlucken am Anfang).
- **Nachher:** Am Anfang jedes Agent-Turns werden erst 3 Chunks gesammelt (Pre-Buffer), bevor die Wiedergabe startet. Output-Latenz von `'low'` auf `0.05` (50ms) erhöht.
- **Warum:** Die ersten Audio-Chunks eines neuen Turns kommen unregelmäßig an (Netzwerk-Jitter). Ohne Pre-Buffer versucht PortAudio sofort zu spielen und hat keinen Nachschub → Knackser/Stotterer. 3 Chunks Pre-Buffer (~30-60ms je nach Chunk-Größe) geben genug Polster für flüssige Wiedergabe.
- **Details:** 
  - `new_turn()` Methode hinzugefügt, wird vom ResponseHandler bei jedem neuen Turn aufgerufen
  - `clear_output()` setzt Pre-Buffer zurück bei Unterbrechungen
  - Fallback: Wenn Queue leer wird bevor Pre-Buffer voll, wird trotzdem abgespielt (für sehr kurze Äußerungen)
