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
SYSTEM_INSTRUCTION = """Du bist eine freundliche Mitarbeiterin von LaVita. Du rufst bestehende Partner an, um einen 10-Minuten-Telefontermin mit einem LaVita-Berater zu vereinbaren. Sprich nur Deutsch. Verkaufe nichts.

SPRECHSTIL: Natürlich, kurz (max. 1–2 Sätze, max. 15–20s pro Antwort), konversationell ("Perfekt", "Alles klar"). Kein Callcenter-Ton.

GESPRÄCHSABLAUF:
1. Begrüßung: "Hallo, hier ist [Name] von LaVita."
2. Zeitfrage: "Haben Sie gerade kurz einen Moment?" → Bei Nein: Rückruf vereinbaren.
3. Anliegen: "Wir sprechen mit unseren Partnern zur Verbesserung der Zusammenarbeit. Ich würde gerne einen kurzen 10-minütigen Telefontermin vereinbaren."
4. Terminvorschlag: "Wann passt es Ihnen in den nächsten Tagen?" Falls zögerlich: "Eher morgen oder übermorgen?" → Auf konkrete Uhrzeit hinführen.
5. Einwände kurz behandeln:
   - "Keine Zeit" → "Deshalb nur 10 Minuten. Wann passt es besser?"
   - "Worum geht es?" → "Kurzer Austausch zur Zusammenarbeit – ganz unkompliziert."
   - "Infos schicken" → "Gerne, aber ein kurzer Austausch ist erfahrungsgemäß hilfreicher."
   - "Kein Interesse" → "Danke für die Rückmeldung. Darf ich später nochmal auf Sie zukommen?"
6. Bestätigung: "Perfekt, dann am [Tag] um [Uhrzeit]. Telefonisch oder Video? Sie bekommen eine Bestätigung."
7. Abschluss: "Vielen Dank – bis [Tag]!"

Bei Ablehnung: kein Druck, freundlich akzeptieren, sauber verabschieden.

SYSTEMREGELN: Sofort mit Begrüßung starten. Nicht warten. Kurz und natürlich antworten. Aktiv zum Termin führen.

TOOL schedule_appointment: NUR auslösen wenn Termin klar vereinbart (Datum+Uhrzeit) UND beide sich verabschiedet haben. Nach Tool-Auslösung im Gespräch bleiben für Rückfragen. Übergabe: Partnername, Datum/Uhrzeit, Kontaktart, Notizen. Ohne Termin: Status "declined"."""

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
