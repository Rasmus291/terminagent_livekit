import os
import datetime
import logging

from config import MODEL_ID

logger = logging.getLogger(__name__)


def generate_summary(client, transcript):
    """Erzeugt eine KI-Zusammenfassung des Gesprächs via Gemini (non-live)."""
    if not transcript:
        return "*Kein Transkript für Zusammenfassung vorhanden.*"
    
    transcript_text = "\n".join(transcript)
    prompt = f"""Fasse das folgende Telefongespräch zwischen einem LaVita-Agenten und einem Partner kurz und prägnant auf Deutsch zusammen. 
Nenne die wichtigsten besprochenen Punkte, das Ergebnis (Termin vereinbart/abgelehnt/Rückruf) und eventuelle nächste Schritte.

Transkript:
{transcript_text}

Zusammenfassung:"""
    
    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt
        )
        return response.text.strip()
    except Exception as e:
        logger.error(f"Zusammenfassung konnte nicht generiert werden: {e}")
        return f"*Fehler bei der Zusammenfassung: {e}*"


def save_session_report(transcript, crm_data=None, latency_data=None,
                        call_duration=None, call_start_time=None, summary=None):
    """Speichert den vollständigen Session Report als Markdown-Datei."""
    os.makedirs("sessions", exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
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
        
        # KI-Zusammenfassung
        if summary:
            f.write("## Zusammenfassung\n\n")
            f.write(f"{summary}\n\n")
        
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
