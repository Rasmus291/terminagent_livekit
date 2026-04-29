import datetime
import glob
import json
import logging
import os
import re
from collections import Counter

from config import MODEL_ID

logger = logging.getLogger(__name__)


def _extract_speaker_lines(transcript, speaker):
    pattern = re.compile(rf"^\*\*\[[^\]]+\]\s*{speaker}:\*\*\s*(.+)$", re.IGNORECASE)
    lines = []
    for entry in transcript or []:
        match = pattern.match(entry.strip())
        if match:
            lines.append(match.group(1).strip())
    return lines


def _fallback_analysis(transcript, reason="Modellanalyse nicht verfügbar"):
    user_lines = _extract_speaker_lines(transcript, "User")
    agent_lines = _extract_speaker_lines(transcript, "Agent")
    user_text = " ".join(user_lines).lower()
    full_text = " ".join((transcript or [])).lower()

    declined_keywords = ["kein interesse", "nicht interessiert", "nein danke", "kein bedarf", "auflegen"]
    callback_keywords = ["später", "rückruf", "zurückrufen", "später anrufen", "anderer termin"]
    scheduled_keywords = ["termin ist eingetragen", "bis zum termin", "bis morgen", "perfekt", "passt also"]
    positive_keywords = ["ja", "gern", "okay", "passt", "danke", "gut", "perfekt"]
    negative_keywords = ["nein", "kein interesse", "nicht", "später", "auflegen", "schlecht"]

    accepted_appointment = any(keyword in user_text for keyword in ["passt", "ja", "gern", "okay"]) and any(
        keyword in full_text for keyword in ["termin", "morgen", "uhr", "freitag", "montag"]
    )

    if any(keyword in full_text for keyword in declined_keywords):
        result = "declined"
    elif any(keyword in full_text for keyword in callback_keywords):
        result = "callback"
    elif any(keyword in full_text for keyword in scheduled_keywords) or accepted_appointment:
        result = "scheduled"
    else:
        result = "unbekannt"

    positive_hits = sum(user_text.count(keyword) for keyword in positive_keywords)
    negative_hits = sum(user_text.count(keyword) for keyword in negative_keywords)
    sentiment_score = max(1, min(10, 5 + positive_hits - negative_hits)) if user_lines else None

    if sentiment_score is None:
        sentiment_total = "unbekannt"
    elif sentiment_score >= 7:
        sentiment_total = "positiv"
    elif sentiment_score <= 4:
        sentiment_total = "negativ"
    else:
        sentiment_total = "neutral"

    if result == "scheduled":
        summary_core = "Das Gespräch endete voraussichtlich mit einer Terminvereinbarung."
        details = "Partner wirkte grundsätzlich kooperativ und gesprächsbereit."
    elif result == "callback":
        summary_core = "Das Gespräch deutet auf einen Rückruf oder einen späteren Kontakt hin."
        details = "Partner signalisierte eher Zeitmangel oder Vertagungswunsch."
    elif result == "declined":
        summary_core = "Das Gespräch endete voraussichtlich ohne Terminvereinbarung."
        details = "Partner wirkte eher ablehnend oder wenig interessiert."
    else:
        summary_core = "Aus dem Transkript lässt sich kein eindeutiges Ergebnis ableiten."
        details = "Stimmung und Ergebnis konnten nur grob heuristisch geschätzt werden."

    if user_lines:
        summary_core += f" Letzte Partneraussage: \"{user_lines[-1][:160]}\"."
    elif agent_lines:
        summary_core += " Es liegt nur Agent-Text ohne klare Partnerantwort vor."

    summary = f"{summary_core} ({reason})"

    return {
        "zusammenfassung": summary,
        "sentiment_partner": sentiment_score,
        "sentiment_gesamt": sentiment_total,
        "stimmung_details": details,
        "ergebnis": result,
    }


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
        return _fallback_analysis(transcript, reason="API-Key fehlt für Modellanalyse")

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
                return _fallback_analysis(transcript, reason="Antwort war kein valides JSON")
            except Exception as e:
                if attempt < 2 and ("503" in str(e) or "UNAVAILABLE" in str(e) or "429" in str(e)):
                    import time

                    logger.warning("Analyse Versuch %s fehlgeschlagen, wiederhole...", attempt + 1)
                    time.sleep(5 * (attempt + 1))
                    continue
                raise
    except Exception as e:
        logger.error("Analyse konnte nicht generiert werden: %s", e)
        return _fallback_analysis(transcript, reason=f"Modellanalyse fehlgeschlagen: {e}")


def save_session_report(
    transcript,
    crm_data=None,
    call_duration=None,
    call_start_time=None,
    analysis=None,
    timestamp=None,
):
    os.makedirs("sessions", exist_ok=True)
    timestamp = timestamp or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
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
