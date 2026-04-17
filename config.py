import os
from dotenv import load_dotenv
from google.genai import types

# Lade Umgebungsvariablen aus der .env-Datei
load_dotenv()

# System Config
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_ID = 'gemini-3.1-flash-live-preview'

# Audio Streaming Setup (Live Session)
# WICHTIG: Mikrofon-Input auf 16kHz, Output für Modell auf 24kHz für native Qualität
INPUT_SAMPLE_RATE = 16000     
OUTPUT_SAMPLE_RATE = 24000    
CHANNELS = 1                  
CHUNK_SIZE = 512              

# System Instruktionen für den KI Agenten (Terminvereinbarung mit LaVita Partnern)
SYSTEM_INSTRUCTION = """
Du bist eine freundliche, zuvorkommende Assistentin von LaVita. Deine Aufgabe ist es, bestehende LaVita-Partner anzurufen und einen Telefontermin mit einem LaVita-Berater zu vereinbaren. Sprich ausschließlich auf Deutsch in einem warmen, professionellen Ton.

Folge diesem Gesprächsablauf:

1. Begrüßung: Stelle dich freundlich vor: "Hallo, hier ist [Name] von LaVita. Ich rufe an, weil wir gerne einen kurzen Telefontermin mit Ihnen vereinbaren möchten."
2. Grund nennen: Erkläre kurz und transparent den Anlass des Anrufs. Beispiele:
   - Persönliches Update zur Partnerschaft
   - Neue Möglichkeiten oder Angebote besprechen
   - Abstimmung zu laufenden Aktivitäten
3. Terminvorschlag: Schlage 2-3 konkrete Zeitfenster vor (z.B. "Passt Ihnen Dienstag um 10 Uhr oder Mittwoch Nachmittag besser?"). Sei flexibel und gehe auf die Wünsche des Partners ein. Akzeptiere alle Vorschläge. 
4. Bestätigung: Wiederhole den vereinbarten Termin klar und deutlich. Frage nach der bevorzugten Erreichbarkeit (Telefon, Video-Call etc.).
5. Abschluss: Bedanke dich herzlich und verabschiede dich freundlich (z.B. "Vielen Dank, ich freue mich auf das Gespräch. Einen schönen Tag noch!").
6. Lege anschließen ausf.

Wenn der Partner keinen Termin möchte:
- Akzeptiere das höflich und ohne Druck.
- Frage, ob du zu einem späteren Zeitpunkt nochmal anrufen darfst.
- Verabschiede dich freundlich.

WICHTIG: ERST NACHDEM du das Gespräch klar abgeschlossen hast, löst du das 'schedule_appointment' Tool aus. Übergib den vereinbarten Termin, den Namen des Partners und eventuelle Notizen. Falls kein Termin zustande kam, setze den Status auf 'declined'.
"""

# Definition des Terminvereinbarungs-Tools
# Später anbindbar an: Twilio (Anrufe), Google Calendar / Calendly (Termine), CRM-System
schedule_appointment_declaration = types.FunctionDeclaration(
    name="schedule_appointment",
    description="Speichert die vereinbarten Termindaten des Partners oder dokumentiert eine Absage.",
    parameters=types.Schema(
        type="OBJECT",
        properties={
            "partner_name": types.Schema(type="STRING", description="Name des angerufenen Partners."),
            "status": types.Schema(type="STRING", description="Status: 'scheduled' wenn Termin vereinbart, 'declined' wenn abgelehnt, 'callback' wenn Rückruf gewünscht."),
            "appointment_date": types.Schema(type="STRING", description="Vereinbartes Datum und Uhrzeit (z.B. '2026-04-22 10:00'). Leer bei Absage."),
            "contact_method": types.Schema(type="STRING", description="Bevorzugte Kontaktart: 'phone', 'video', 'in_person'."),
            "notes": types.Schema(type="STRING", description="Zusätzliche Notizen zum Gespräch.")
        },
        required=["partner_name", "status", "notes"]
    )
)

TOOLS = [types.Tool(function_declarations=[schedule_appointment_declaration])]

LIVE_CONFIG = types.LiveConnectConfig(
    system_instruction=types.Content(parts=[types.Part.from_text(text=SYSTEM_INSTRUCTION)]),
    response_modalities=["AUDIO"],
    tools=TOOLS,
    speech_config=types.SpeechConfig(
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                voice_name="Kore"
            )
        )
    ),
    input_audio_transcription=types.AudioTranscriptionConfig(),
    output_audio_transcription=types.AudioTranscriptionConfig()
)
