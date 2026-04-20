import os
import json
import datetime
import logging

from config_pipecat import MODEL_ID

logger = logging.getLogger(__name__)


def generate_analysis(transcript):
    """Erzeugt Zusammenfassung + Sentiment-Analyse via Gemini.

    Nutzt einen separaten (nicht-Live) Gemini Call für die Analyse.
    """
    if not transcript:
        return {
            "zusammenfassung": "*Kein Transkript für Analyse vorhanden.*",
            "sentiment_partner": None,
            "sentiment_gesamt": "unbekannt",
            "stimmung_details": "",
            "ergebnis": "unbekannt",
        }

    from google import genai

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return {
            "zusammenfassung": "*API Key fehlt für Analyse.*",
            "sentiment_partner": None,
            "sentiment_gesamt": "unbekannt",
            "stimmung_details": "",
            "ergebnis": "unbekannt",
        }

    client = genai.Client(api_key=api_key)
    transcript_text = "\n".join(transcript)

    prompt = f"""Analysiere das folgende Telefongespräch zwischen einem LaVita-Agenten und einem Partner.

Antworte NUR mit validem JSON (kein Markdown, keine Code-Blöcke), exakt in diesem Format:
{{
  "zusammenfassung": "Kurze, prägnante Zusammenfassung auf Deutsch (2-4 Sätze). Wichtigste Punkte und Ergebnis.",
  "sentiment_partner": 7,
  "sentiment_gesamt": "positiv",
  "stimmung_details": "Kurze Beschreibung der Stimmung des Partners, z.B. interessiert, gestresst, ablehnend",
  "ergebnis": "scheduled"
}}

Felder:
- sentiment_partner: 1-10 (1=sehr negativ, 10=sehr positiv)
- sentiment_gesamt: "positiv", "neutral" oder "negativ"
- ergebnis: "scheduled", "declined", "callback" oder "unbekannt"

Transkript:
{transcript_text}"""

    # Fallback-Modelle falls das primäre Modell überlastet ist
    analysis_models = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-lite"]

    try:
        for attempt in range(3):
            model_name = analysis_models[min(attempt, len(analysis_models) - 1)]
            try:
                response = client.models.generate_content(
                    model=model_name, contents=prompt
                )
                raw = response.text.strip()
                if raw.startswith("```"):
                    raw = raw.split("```")[1]
                    if raw.startswith("json"):
                        raw = raw[4:]
                    raw = raw.strip()
                return json.loads(raw)
            except json.JSONDecodeError:
                logger.warning(f"JSON-Parsing fehlgeschlagen: {response.text[:200]}")
                return {
                    "zusammenfassung": response.text.strip(),
                    "sentiment_partner": None,
                    "sentiment_gesamt": "unbekannt",
                    "stimmung_details": "",
                    "ergebnis": "unbekannt",
                }
            except Exception as e:
                if attempt < 2 and ("503" in str(e) or "UNAVAILABLE" in str(e) or "429" in str(e)):
                    logger.warning(f"Analyse Versuch {attempt+1} fehlgeschlagen, wiederhole...")
                    import time
                    time.sleep(5 * (attempt + 1))
                    continue
                raise
    except Exception as e:
        logger.error(f"Analyse konnte nicht generiert werden: {e}")
        return {
            "zusammenfassung": f"*Fehler bei der Analyse: {e}*",
            "sentiment_partner": None,
            "sentiment_gesamt": "unbekannt",
            "stimmung_details": "",
            "ergebnis": "unbekannt",
        }


def save_session_report(
    transcript,
    crm_data=None,
    call_duration=None,
    call_start_time=None,
    analysis=None,
):
    """Speichert den vollständigen Session Report als Markdown-Datei."""
    os.makedirs("sessions", exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"sessions/session_{timestamp}.md"

    with open(filename, "w", encoding="utf-8") as f:
        f.write("# Session Report (Pipecat)\n\n")

        f.write("## Anruf-Details\n")
        f.write(
            f"- **Datum & Uhrzeit:** {call_start_time or datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        f.write(f"- **Modell:** {MODEL_ID}\n")
        f.write("- **Framework:** Pipecat + GeminiLiveLLMService\n")
        if call_duration is not None:
            minutes, seconds = divmod(int(call_duration), 60)
            f.write(f"- **Gesprächsdauer:** {minutes}:{seconds:02d} min\n")
        f.write("\n")

        if crm_data:
            f.write("## Termindaten\n")
            f.write(f"- **Partner:** {crm_data.get('partner_name', 'N/A')}\n")
            f.write(f"- **Status:** {crm_data.get('status', 'N/A')}\n")
            f.write(f"- **Termin:** {crm_data.get('appointment_date', 'N/A')}\n")
            f.write(f"- **Kontaktart:** {crm_data.get('contact_method', 'N/A')}\n")
            f.write(f"- **Notizen:** {crm_data.get('notes', 'N/A')}\n\n")

        if analysis and isinstance(analysis, dict):
            f.write("## Zusammenfassung\n\n")
            f.write(f"{analysis.get('zusammenfassung', '')}\n\n")

            f.write("## Sentiment-Analyse\n")
            f.write(
                f"- **Gesamtstimmung:** {analysis.get('sentiment_gesamt', 'unbekannt')}\n"
            )
            sentiment_score = analysis.get("sentiment_partner")
            if sentiment_score is not None:
                f.write(f"- **Partner-Stimmung:** {sentiment_score}/10\n")
            f.write(f"- **Details:** {analysis.get('stimmung_details', '-')}\n")
            f.write(f"- **Ergebnis:** {analysis.get('ergebnis', 'unbekannt')}\n\n")

        f.write("## Transkript\n\n")
        if not transcript:
            f.write("*Kein Transkript vorhanden.*\n")
        else:
            for line in transcript:
                f.write(f"{line}\n\n")

    logger.info(f"Session Report gespeichert: {filename}")
    return filename
