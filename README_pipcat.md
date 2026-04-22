# LiveKit-Anbindung (statt Pipecat)

Diese Projektvariante nutzt **LiveKit Agents** mit **Gemini Live API** als Sprachmodell.

## Relevante Dateien
- `main_livekit.py`: LiveKit-Agent-Einstiegspunkt (Room-Worker)
- `tool_handler_livekit.py`: Tool-Logik (`check_availability`, `schedule_appointment`, `end_call`)
- `requirements-pipcat.txt`: LiveKit-Dependencies
- `main.py`: Direkte Gemini-Variante (Fallback, weiterhin nutzbar)

## Setup
1. Abhängigkeiten installieren:
   
   `pip install -r requirements-pipcat.txt`

2. `.env` konfigurieren:
   
   `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `GEMINI_API_KEY`

   Optional:
   `LIVEKIT_AGENT_NAME` (Default: `lavita-agent`)
   `LIVEKIT_GEMINI_VOICE` (Default: `Kore`)

3. Agent starten:
   
   `python main_livekit.py dev`

## Hinweise
- Die Tool-Integrationen für Calendly und E-Mail bleiben erhalten.
- Session-Reports werden weiterhin in `sessions/` geschrieben.