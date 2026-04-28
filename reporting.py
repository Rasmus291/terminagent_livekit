import os
import json
import asyncio
import datetime
import logging
import re

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


async def generate_analysis(client, transcript):
    """Erzeugt Zusammenfassung + Sentiment-Analyse als strukturiertes Dict via Gemini."""
    if not transcript:
        return {
            "zusammenfassung": "*Kein Transkript für Analyse vorhanden.*",
            "sentiment_partner": None,
            "sentiment_gesamt": "unbekannt",
            "stimmung_details": "",
            "ergebnis": "unbekannt"
        }
    
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
    
    try:
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt
                )
                raw = response.text.strip()
                # JSON aus eventuellen Code-Blöcken extrahieren
                if raw.startswith("```"):
                    raw = raw.split("```")[1]
                    if raw.startswith("json"):
                        raw = raw[4:]
                    raw = raw.strip()
                return json.loads(raw)
            except json.JSONDecodeError:
                logger.warning(f"JSON-Parsing fehlgeschlagen, Rohtext: {response.text[:200]}")
                return _fallback_analysis(transcript, reason="Antwort war kein valides JSON")
            except Exception as e:
                if attempt < 2 and ("503" in str(e) or "UNAVAILABLE" in str(e) or "429" in str(e)):
                    logger.warning(f"Analyse Versuch {attempt+1} fehlgeschlagen, wiederhole...")
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                raise
    except Exception as e:
        logger.error(f"Analyse konnte nicht generiert werden: {e}")
        return _fallback_analysis(transcript, reason=f"Modellanalyse fehlgeschlagen: {e}")


def save_session_report(transcript, crm_data=None, latency_data=None,
                        call_duration=None, call_start_time=None, analysis=None,
                        timestamp=None):
    """Speichert den vollständigen Session Report als Markdown-Datei."""
    os.makedirs("sessions", exist_ok=True)
    timestamp = timestamp or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"sessions/session_{timestamp}.md"
    
    with open(filename, "w", encoding="utf-8") as f:
        f.write("# Session Report\n\n")
        
        # Anruf-Metadaten
        f.write("## Anruf-Details\n")
        f.write(f"- **Datum & Uhrzeit:** {call_start_time or datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"- **Modell:** {MODEL_ID}\n")
        if call_duration is not None:
            minutes, seconds = divmod(int(call_duration), 60)
            f.write(f"- **Gesprächsdauer:** {minutes}:{seconds:02d} min\n")
        f.write("\n")
        
        # Termindaten / CRM
        if crm_data:
            f.write("## Termindaten\n")
            f.write(f"- **Partner:** {crm_data.get('partner_name', 'N/A')}\n")
            f.write(f"- **Status:** {crm_data.get('status', 'N/A')}\n")
            f.write(f"- **Termin:** {crm_data.get('appointment_date', 'N/A')}\n")
            f.write(f"- **Kontaktart:** {crm_data.get('contact_method', 'N/A')}\n")
            f.write(f"- **Notizen:** {crm_data.get('notes', 'N/A')}\n\n")
        
        # KI-Zusammenfassung + Sentiment
        if analysis and isinstance(analysis, dict):
            f.write("## Zusammenfassung\n\n")
            f.write(f"{analysis.get('zusammenfassung', '')}\n\n")
            
            f.write("## Sentiment-Analyse\n")
            f.write(f"- **Gesamtstimmung:** {analysis.get('sentiment_gesamt', 'unbekannt')}\n")
            sentiment_score = analysis.get('sentiment_partner')
            if sentiment_score is not None:
                f.write(f"- **Partner-Stimmung:** {sentiment_score}/10\n")
            f.write(f"- **Details:** {analysis.get('stimmung_details', '-')}\n")
            f.write(f"- **Ergebnis:** {analysis.get('ergebnis', 'unbekannt')}\n\n")
        
        # Latenz-Statistiken
        if latency_data and len(latency_data) > 0:
            avg = sum(latency_data) / len(latency_data)
            f.write("## Latenz-Statistiken\n")
            f.write(f"- **Durchschnitt:** {avg:.0f}ms\n")
            f.write(f"- **Min:** {min(latency_data):.0f}ms\n")
            f.write(f"- **Max:** {max(latency_data):.0f}ms\n")
            f.write(f"- **Messungen:** {len(latency_data)}\n\n")
        
        # Transkript
        f.write("## Transkript\n\n")
        if not transcript:
            f.write("*Kein Transkript vorhanden.*\n")
        else:
            for line in transcript:
                f.write(f"{line}\n\n")
            
    logger.info(f"Session Report in {filename} gespeichert.")
    return filename
