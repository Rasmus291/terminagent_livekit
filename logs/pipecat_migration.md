# Pipecat Migration Log

**Datum:** 2026-04-20
**Projekt:** terminagent
**Ziel:** Migration von direkter Gemini Live API auf Pipecat-Framework bei Beibehaltung von Gemini als All-in-One (STT + LLM + TTS)

---

## Übersicht der Änderungen

### Warum Pipecat?
- **Transport-Abstraktion:** Einfacher Wechsel zwischen lokal, Daily WebRTC, Twilio, LiveKit ohne Code-Änderung
- **Robusteres Interruption-Handling:** Pipecat managed Turn-Detection und Barge-In automatisch
- **Context Management:** Automatische Gesprächshistorie und Context Aggregation
- **Session Resumption:** Automatische Wiederverbindung bei Verbindungsabbrüchen
- **Wartbarkeit:** Weniger eigener Code, Community-Support, aktive Weiterentwicklung
- **Skalierbarkeit:** Einfache Integration von Telephonie (Twilio SIP) für Produktivbetrieb

### Was bleibt gleich?
- **Gemini Live API als All-in-One:** STT + LLM + TTS in einem Modell (kein Pipeline-Split)
- **Stimme:** "Kore" (Gemini native voice)
- **System Prompt:** Identischer LaVita-Terminvereinbarungs-Prompt
- **Function Calling:** `schedule_appointment` Tool mit gleicher Logik
- **Reporting:** Session Reports + Analyse bleiben erhalten
- **API Key:** Gleiche `.env` Konfiguration (GEMINI_API_KEY)

---

## Änderungsprotokoll

### 1. Neue Datei: `requirements-pipcat.txt`
**Was:** Aktualisierte Dependencies für Pipecat
**Warum:** Pipecat ersetzt die manuelle Audio-Verarbeitung und bietet den GeminiLiveLLMService
**Vorher:** Nur `pipcat` (Platzhalter, existiert nicht als echtes Package)
**Nachher:** `pipecat-ai[google,silero]` + bestehende Dependencies

### 2. Neue Datei: `config_pipecat.py`
**Was:** Pipecat-spezifische Konfiguration mit GeminiLiveLLMService Settings
**Warum:** Pipecat nutzt eigene Settings-Klassen statt der direkten google.genai.types Konfiguration
**Beibehaltung:** System Prompt, API Key, Tool-Definition — alles identisch
**Änderung:** Tool-Definition nutzt Pipcats FunctionSchema statt google.genai.types.FunctionDeclaration; LiveConnectConfig wird durch GeminiLiveLLMService.Settings ersetzt

### 3. Neue Datei: `main_pipecat.py`
**Was:** Neuer Haupteinstiegspunkt für Pipecat-basiertes System
**Warum:** Ersetzt main.py + audio_handler.py + response_handler.py in einer kompakteren Struktur
**Details:**
- Pipeline: Transport → UserAggregator → GeminiLiveLLM → Transport → AssistantAggregator
- Transport: LocalAudioTransport (Mikrofon/Lautsprecher, wie bisheriges sounddevice)
- Transkription via Pipcats eingebaute Event Handler (on_user_turn_stopped, on_assistant_turn_stopped)
- Automatisches Interruption-Handling (kein manuelles Queue-Clearing mehr)
- Session-Beendigung über Pipeline-Lifecycle statt manuelles asyncio.CancelledError

### 4. Neue Datei: `tool_handler_pipecat.py`
**Was:** Pipecat-kompatible Version des Tool Handlers
**Warum:** Pipecat nutzt `register_function()` + `FunctionCallParams` statt direkter Response-Handling
**Vorher:** Manuelles `session.send_tool_response()` + Logik für Gesprächsende
**Nachher:** Callback-basiert über `params.result_callback()`, Pipecat sendet Tool Response automatisch

### 5. Neue Datei: `reporting_pipecat.py`
**Was:** Reporting-Adapter für Pipecat Event-System
**Warum:** Transkription kommt jetzt aus Pipcats Aggregator-Events statt aus manueller Response-Verarbeitung
**Beibehaltung:** Gleiche Report-Struktur, gleiche Analyse via Gemini, gleiche Dateistruktur

### 6. Bestehende Dateien: NICHT VERÄNDERT
**Was:** main.py, audio_handler.py, response_handler.py, tool_handler.py, config.py, reporting.py
**Warum:** Bisheriger Code bleibt als Fallback erhalten. Beide Systeme können parallel existieren.
- `python main.py` → Direktes Gemini Live API System (wie bisher)
- `python main_pipecat.py` → Neues Pipecat-basiertes System

---

## Architektur-Vergleich

### Vorher (direkt):
```
main.py
  → audio_handler.py (sounddevice, Queue, Recording)
  → response_handler.py (Turn-Processing, Latenz, Interruptions)
  → tool_handler.py (Function Call Handling)
  → config.py (Gemini Config)
  → reporting.py (Session Reports)
```

### Nachher (Pipecat):
```
main_pipecat.py
  → Pipecat Pipeline (Transport, LLM, Aggregators)
  → tool_handler_pipecat.py (Function Call via register_function)
  → reporting_pipecat.py (Reports via Event Handler)
  → config_pipecat.py (Pipecat Settings)
```

### Was Pipecat intern übernimmt (bisher eigener Code):
- Audio I/O (ersetzt audio_handler.py komplett)
- Turn-Detection + Interruption-Handling (ersetzt response_handler.py)
- Latenz-Tracking (Pipcats eingebaute Metrics)
- Transkription-Aggregation (Pipcats Context Aggregators)
- WebSocket-Verbindung zu Gemini (Pipcats Connection Management mit Auto-Reconnect)

---

## Bekannte Einschränkungen
- **Stereo-Recording:** Pipcats LocalAudioTransport hat kein eingebautes Stereo-Recording (Partner links, Agent rechts). Falls benötigt, muss ein Custom FrameProcessor erstellt werden.
- **Feine Latenz-Messung:** Pipcats Metrics sind allgemeiner als die bisherige per-Turn Messung.

---

## Erstellte Dateien

| Datei | Beschreibung |
|---|---|
| `config_pipecat.py` | Pipecat-Konfiguration (Settings, Tools, System Prompt) |
| `main_pipecat.py` | Haupteinstiegspunkt mit Pipeline-Setup |
| `tool_handler_pipecat.py` | Function Call Handler für schedule_appointment |
| `reporting_pipecat.py` | Session Reports + Sentiment-Analyse |
| `requirements-pipcat.txt` | Aktualisierte Dependencies |
| `logs/pipecat_migration.md` | Diese Logdatei |

## Validierung
- Alle Imports erfolgreich getestet ✓
- Pipecat 1.0.0 installiert ✓
- GeminiLiveLLMService mit de-DE, Kore, schedule_appointment konfiguriert ✓
- Bisheriger Code (`main.py`, `audio_handler.py`, etc.) unverändert ✓

## Nutzung
```bash
# Neues Pipecat-System (lokal, Mikrofon/Lautsprecher):
python main_pipecat.py

# Twilio-Server (echte Telefonanrufe):
python main_twilio.py
# Dann ngrok starten: ngrok http 8765
# Twilio Webhook setzen auf: https://<ngrok-url>/twilio/incoming

# Ausgehenden Anruf starten:
# POST http://localhost:8765/twilio/call  Body: {"to": "+49170XXXXXXX"}

# Bisheriges System (Fallback):
python main.py
```

---

## Twilio-Integration (2026-04-20)

### Neue Datei: `main_twilio.py`
**Was:** FastAPI-Server der Twilio Media Streams empfängt und an die Pipecat Pipeline weiterleitet
**Warum:** Ermöglicht echte Telefonanrufe über das Telefonnetz statt nur lokalem Mikrofon
**Details:**
- FastAPI App mit 3 Endpoints:
  - `POST /twilio/incoming` — Webhook für eingehende/verbundene Anrufe (liefert TwiML)
  - `WS /twilio/ws` — WebSocket für Twilio Media Streams (Audio-Daten)
  - `POST /twilio/call` — REST-Endpoint um ausgehende Anrufe zu starten
  - `GET /health` — Health-Check
- Nutzt `FastAPIWebsocketTransport` + `TwilioFrameSerializer` statt `LocalAudioTransport`
- Gleiche Pipeline wie main_pipecat.py (Gemini Live All-in-One, identischer Agent)
- Audio-Recording, Transkription, Session Reports — alles identisch
- AMD (Answering Machine Detection) bei ausgehenden Anrufen aktiviert
- Auto-Hangup: Twilio-Anruf wird automatisch beendet wenn Pipeline endet

### Geänderte Datei: `requirements-pipcat.txt`
**Was:** Neue Dependencies hinzugefügt
**Warum:** FastAPI (Web-Server), uvicorn (ASGI Runner), twilio (REST API SDK), websocket Extra für Pipecat

### .env Erweiterung (manuell nötig)
Diese Variablen müssen in der `.env` ergänzt werden:
```
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_PHONE_NUMBER=+49xxxxxxxxxx
```
