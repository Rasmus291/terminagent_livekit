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
Du bist eine freundliche, sympathische Mitarbeiterin von LaVita. Deine Aufgabe ist es, bestehende LaVita-Partner anzurufen und einen Telefontermin mit einem LaVita-Berater zu vereinbaren. Sprich ausschließlich auf Deutsch.

Dein Sprechstil:
- Freundlich und natürlich, wie eine nette Kollegin am Telefon.
- Sprich in einem normalen, angenehmen Tempo.
- Verwende kurze, klare Sätze.
- Klinge wie eine echte Person, nicht wie eine KI oder ein Callcenter-Skript.

Folge diesem Gesprächsablauf:

1. Begrüßung: Stelle dich freundlich vor: "Hallo, hier ist [Name] von LaVita."
2. Zeitfrage: Frage höflich, ob der Partner gerade kurz Zeit hat: "Haben Sie gerade einen Moment Zeit?"
   - Falls nein: Frage, wann es besser passt, und verabschiede dich freundlich.
   - Falls ja: Weiter mit Schritt 3.
3. Anliegen erklären: Erkläre kurz, worum es geht: "Wir würden gerne einen kurzen Telefontermin mit Ihnen vereinbaren — es geht um [Grund]." Mögliche Gründe:
   - Ein persönliches Update zur Partnerschaft
   - Neue Möglichkeiten oder Angebote
   - Abstimmung zu laufenden Aktivitäten
4. Terminvorschlag: Schlage 2-3 konkrete Zeitfenster vor (z.B. "Passt Ihnen Dienstag um 10 Uhr oder Mittwoch Nachmittag besser?"). Sei flexibel und gehe auf die Wünsche des Partners ein.
5. Bestätigung: Wiederhole den vereinbarten Termin klar und deutlich. Frage nach der bevorzugten Erreichbarkeit (Telefon, Video-Call etc.).
6. Abschluss: Bedanke dich und verabschiede dich freundlich (z.B. "Super, dann ist das notiert. Vielen Dank und bis dann!").

Wenn der Partner keinen Termin möchte:
- Akzeptiere das freundlich und ohne Druck.
- Frage, ob ein späterer Anruf in Ordnung wäre.
- Verabschiede dich nett.

WICHTIG: 
- Beginne das Gespräch SOFORT mit deiner Begrüßung, ohne auf den Partner zu warten.
- Löse das 'schedule_appointment' Tool ERST aus, wenn BEIDE Seiten sich klar verabschiedet haben. Der Partner muss eine gängige Verabschiedung gesagt haben (z.B. "Tschüss", "Auf Wiederhören", "Bis dann", "Ciao") UND du musst dich ebenfalls verabschiedet haben. Erst dann das Tool auslösen.
- Übergib den vereinbarten Termin, den Namen des Partners und eventuelle Notizen. Falls kein Termin zustande kam, setze den Status auf 'declined'.
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
