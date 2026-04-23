import datetime
import glob
import json
import logging
import os
import re
from collections import Counter

from config import MODEL_ID

logger = logging.getLogger(__name__)


def build_learning_brief(max_sessions=20):
    try:
        files = sorted(glob.glob("sessions/session_*.md"), reverse=True)[:max_sessions]
        if not files:
            return ""

        result_counter = Counter()
        objection_counter = Counter()

        for path in files:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                continue

            result_match = re.search(r"- \*\*Ergebnis:\*\*\s*([^\n]+)", content, re.IGNORECASE)
            if result_match:
                result_value = result_match.group(1).strip().lower()
                if "scheduled" in result_value:
                    result_counter["scheduled"] += 1
                elif "callback" in result_value:
                    result_counter["callback"] += 1
                elif "declined" in result_value:
                    result_counter["declined"] += 1
                else:
                    result_counter["unknown"] += 1

            user_lines = re.findall(r"\*\*\[[^\]]+\]\s*User:\*\*\s*(.+)", content)
            for line in user_lines:
                text = line.lower()
                if any(k in text for k in ["keine zeit", "jetzt schlecht", "zu tun", "später"]):
                    objection_counter["keine_zeit"] += 1
                if any(k in text for k in ["kein interesse", "nicht interessiert", "nein danke"]):
                    objection_counter["kein_interesse"] += 1
                if any(k in text for k in ["worum", "worum geht", "infos", "informationen"]):
                    objection_counter["worum_gehts"] += 1

        total = sum(result_counter.values())
        if total == 0:
            return ""

        lines = [
            "Interne Lernnotizen aus vergangenen Gesprächen (nur intern, NICHT vorlesen):",
            f"- Ausgewertete Sessions: {total}",
            f"- Ergebnisse: scheduled={result_counter['scheduled']}, callback={result_counter['callback']}, declined={result_counter['declined']}",
            "- Regeln: Immer direkt mit Begrüßung + Terminvorschlag starten; keine Zeitfrage am Anfang.",
            "- Regeln: Niemals abrupt auflegen; bei Absage immer bedanken, freundlich verabschieden, dann end_call.",
        ]

        if objection_counter["keine_zeit"] > 0:
            lines.append(
                "- Häufiger Einwand: 'keine Zeit' → sehr kurze Rückrufoption anbieten ODER direkt höflich verabschieden."
            )
        if objection_counter["kein_interesse"] > 0:
            lines.append(
                "- Häufiger Einwand: 'kein Interesse' → nicht diskutieren, wertschätzend abschließen."
            )
        if objection_counter["worum_gehts"] > 0:
            lines.append(
                "- Häufige Rückfrage 'Worum geht es?' → in 1 Satz antworten: kurzer Austausch zur besseren Zusammenarbeit."
            )

        lines.append("- Antworte kurz, ohne lange Pausen zwischen den Sätzen.")
        return "\n".join(lines)
    except Exception as e:
        logger.warning("Lernkontext konnte nicht erstellt werden: %s", e)
        return ""


def generate_analysis(transcript):
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

    analysis_models = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-lite"]

    try:
        for attempt in range(3):
            model_name = analysis_models[min(attempt, len(analysis_models) - 1)]
            try:
                response = client.models.generate_content(model=model_name, contents=prompt)
                raw = response.text.strip()
                if raw.startswith("```"):
                    raw = raw.split("```")[1]
                    if raw.startswith("json"):
                        raw = raw[4:]
                    raw = raw.strip()
                return json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("JSON-Parsing fehlgeschlagen: %s", response.text[:200])
                return {
                    "zusammenfassung": response.text.strip(),
                    "sentiment_partner": None,
                    "sentiment_gesamt": "unbekannt",
                    "stimmung_details": "",
                    "ergebnis": "unbekannt",
                }
            except Exception as e:
                if attempt < 2 and ("503" in str(e) or "UNAVAILABLE" in str(e) or "429" in str(e)):
                    import time

                    logger.warning("Analyse Versuch %s fehlgeschlagen, wiederhole...", attempt + 1)
                    time.sleep(5 * (attempt + 1))
                    continue
                raise
    except Exception as e:
        logger.error("Analyse konnte nicht generiert werden: %s", e)
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
    os.makedirs("sessions", exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"sessions/session_{timestamp}.md"

    with open(filename, "w", encoding="utf-8") as f:
        f.write("# Session Report (LiveKit)\n\n")

        f.write("## Anruf-Details\n")
        f.write(
            f"- **Datum & Uhrzeit:** {call_start_time or datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        f.write(f"- **Modell:** {MODEL_ID}\n")
        f.write("- **Framework:** LiveKit Agents + Gemini Realtime\n")
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
            f.write(f"- **Gesamtstimmung:** {analysis.get('sentiment_gesamt', 'unbekannt')}\n")
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

    logger.info("Session Report gespeichert: %s", filename)
    return filename
