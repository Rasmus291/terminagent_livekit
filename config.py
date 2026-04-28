import os
from dotenv import load_dotenv
from google.genai import types

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_ID = os.getenv("MODEL_ID", "gemini-2.5-flash-native-audio-latest")
VOICE_NAME = os.getenv("VOICE_NAME", "Aoede")
ENABLE_FUNCTION_TOOLS = os.getenv("ENABLE_FUNCTION_TOOLS", "0").strip().lower() in {"1", "true", "yes"}

INPUT_SAMPLE_RATE = 16000     
OUTPUT_SAMPLE_RATE = 24000    
CHANNELS = 1                  
CHUNK_SIZE = 512              

# Agent-Instruktionen: kurz, direkt, keine technischen Details
SYSTEM_INSTRUCTION = """Du bist Anna, eine freundliche Mitarbeiterin von LaVita. Deine einzige Aufgabe: Vereinbare einen 10-Minuten-Telefontermin mit dem Partner.

REGELN:
1. Starte SOFORT mit Begrüßung + Anliegen (ohne konkreten Terminslot), frage lediglich, ob in den nächsten tagen der Partner Zeit hat. 
2. Sprich klar und deutlich auf Deutsch in normalem Sprechtempo. Max. 1-2 Sätze pro Antwort.
3. Führe aktiv zum konkreten Termin (Datum + Uhrzeit). Termine sind immer telefonisch.
4. Sobald Termin mit Datum/Uhrzeit bestätigt: NICHT erneut nach Termin fragen. Kurz bestätigen und verabschieden.
5. Bei Absage genau EINMAL fragen: "Wäre es für Sie in Ordnung, wenn wir uns in etwa sechs Monaten noch einmal kurz melden?" Antwort akzeptieren.
6. Verabschiedung: nur EINE kurze Formel (z.B. "Vielen Dank, bis zum Termin."). Danach sofort Stille.
7. NIEMALS über interne Prozesse, Tools oder technische Details sprechen.
8. "Vor Ort" und "Video" sind nicht erlaubt. Nur Telefon.

EINWÄNDE (kurz antworten):
- Keine Zeit → "Nur 10 Minuten. Passt ein anderer Tag besser?"
- Worum geht es → "Kurzer Austausch zur Partnerschaft. Details klären wir im Gespräch."
- Kein Interesse → 6-Monats-Rückfrage (siehe Regel 5)
- Infos schicken → "Gerne, aber ein Austausch ist hilfreicher. Nur 10 Minuten."

GESPRÄCHSENDE:
- Wenn Partner "Auf Wiedersehen", "Tschüss" etc. sagt: klar verabschieden und SOFORT beenden.
- Keine weiteren Worte nach der Verabschiedung.
"""

schedule_appointment_declaration = types.FunctionDeclaration(
    name="schedule_appointment",
    description="Speichert Termindaten des Partners",
    parameters=types.Schema(
        type="OBJECT",
        properties={
            "partner_name": types.Schema(type="STRING", description="Name des Partners"),
            "status": types.Schema(type="STRING", description="'scheduled' oder 'declined'"),
            "appointment_date": types.Schema(type="STRING", description="Datum und Uhrzeit z.B. 2026-04-23 10:00"),
            "contact_method": types.Schema(type="STRING", description="Nur 'phone'"),
            "notes": types.Schema(type="STRING", description="Notizen zum Gespräch")
        },
        required=["partner_name", "status", "notes"]
    )
)

TOOLS = [types.Tool(function_declarations=[schedule_appointment_declaration])]

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

if ENABLE_FUNCTION_TOOLS:
    _live_config_kwargs["tools"] = TOOLS

LIVE_CONFIG = types.LiveConnectConfig(**_live_config_kwargs)
