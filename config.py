import os
from dotenv import load_dotenv
from google.genai import types

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_ID = os.getenv("MODEL_ID", "gemini-2.5-flash-native-audio-preview-12-2025")
VOICE_NAME = os.getenv("VOICE_NAME", "Aoede")
ENABLE_FUNCTION_TOOLS = os.getenv("ENABLE_FUNCTION_TOOLS", "0").strip().lower() in {"1", "true", "yes"}

INPUT_SAMPLE_RATE = 16000     
OUTPUT_SAMPLE_RATE = 24000    
CHANNELS = 1                  
CHUNK_SIZE = 512              

SYSTEM_INSTRUCTION = """Du bist Anna von LaVita. Dein Ziel: Vereinbare einen 10-min Telefontermin mit dem Partner.

GREETING (wird automatisch ausgelöst):
Grüße freundlich mit Name falls vorhanden, erkläre das Anliegen kurz, frage nach Verfügbarkeit.

REGELN:
1. Deutsch, freundlich, natürlich — wie echter Telefonanruf.
2. Maximal 2-3 Sätze pro Antwort.
3. NUR verfügbare Termine anbieten (Montag–Donnerstag 08:00–17:00, Freitag 08:00–16:00).
4. Einwand "Outside Hours" → Verfügbaren Termin vorschlagen.
5. Nach Terminbestätigung: Sehr kurz bestätigen, verabschieden, STILLE.
6. Bei Absage 1x fragen: "Dürfen wir in 6 Monaten anrufen?" → Antwort akzeptieren.
7. Nach Partner-Verabschiedung: Kurz zurückgrüßen, STILLE.
8. Kein Tech/Prozess-Gerede.

EINWAND-TIPPS:
- "Keine (Zeit)" → "Nur 10 Min. Welcher Tag?"
- "Worum geht's?" → "Kurzer Austausch zur Partnerschaft. Details im Gespräch."
- "Kein Interesse" → Siehe Regel 6.
- "Infos zuerst" → "Austausch hilfreich. Dauert nur 10 Min."
"""

_live_config_kwargs = {
    "system_instruction": types.Content(parts=[types.Part.from_text(text=SYSTEM_INSTRUCTION)]),
    "response_modalities": ["AUDIO"],
    "speech_config": types.SpeechConfig(
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                voice_name=VOICE_NAME
            )
        )
    ),
    "input_audio_transcription": types.AudioTranscriptionConfig(),
    "output_audio_transcription": types.AudioTranscriptionConfig(),
}

LIVE_CONFIG = types.LiveConnectConfig(**_live_config_kwargs)
