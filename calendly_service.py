"""
Calendly API v2 Integration.

Bietet Funktionen zum:
- Abfragen verfügbarer Terminslots
- Erstellen von Einmal-Buchungslinks
- Auslesen der Event Types

Benötigt in .env:
  CALENDLY_API_TOKEN=<Personal Access Token>
  CALENDLY_EVENT_TYPE_URI=<URI des Event Types> (optional, wird automatisch ermittelt)
"""

import os
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

CALENDLY_BASE_URL = "https://api.calendly.com"
CALENDLY_API_TOKEN = os.getenv("CALENDLY_API_TOKEN", "")
CALENDLY_EVENT_TYPE_URI = os.getenv("CALENDLY_EVENT_TYPE_URI", "")

# Verbindliche Bürozeiten (lokale Zeit):
# Montag-Donnerstag 08:00-17:00, Freitag 08:00-16:00, Wochenende geschlossen
WEEKDAY_BOOKING_WINDOWS = {
    0: (8, 17),  # Montag
    1: (8, 17),  # Dienstag
    2: (8, 17),  # Mittwoch
    3: (8, 17),  # Donnerstag
    4: (8, 16),  # Freitag
}
BOOKING_TIMEZONE = "Europe/Berlin"

# Wird beim ersten Aufruf gecacht
_user_uri: str | None = None
_event_type_uri: str | None = None


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {CALENDLY_API_TOKEN}",
        "Content-Type": "application/json",
    }


def is_configured() -> bool:
    return bool(CALENDLY_API_TOKEN)


async def get_user_uri() -> str:
    """Holt die User-URI vom /users/me Endpoint (gecacht)."""
    global _user_uri
    if _user_uri:
        return _user_uri

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{CALENDLY_BASE_URL}/users/me", headers=_headers())
        resp.raise_for_status()
        _user_uri = resp.json()["resource"]["uri"]
        logger.info(f"Calendly User URI: {_user_uri}")
        return _user_uri


async def get_event_type_uri() -> str:
    """Ermittelt die Event Type URI (aus ENV oder erstem aktiven Event Type)."""
    global _event_type_uri
    if _event_type_uri:
        return _event_type_uri

    if CALENDLY_EVENT_TYPE_URI:
        _event_type_uri = CALENDLY_EVENT_TYPE_URI
        return _event_type_uri

    user_uri = await get_user_uri()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{CALENDLY_BASE_URL}/event_types",
            headers=_headers(),
            params={"user": user_uri, "active": "true"},
        )
        resp.raise_for_status()
        event_types = resp.json().get("collection", [])

    if not event_types:
        raise ValueError("Keine aktiven Calendly Event Types gefunden. Bitte erstelle einen in Calendly.")

    _event_type_uri = event_types[0]["uri"]
    logger.info(f"Verwende Event Type: {event_types[0].get('name')} ({_event_type_uri})")
    return _event_type_uri


async def get_available_slots(days_ahead: int = 5) -> list[dict]:
    """
    Holt verfügbare Termine für die nächsten `days_ahead` Tage.

    Returns:
        Liste von Dicts mit 'start_time' und 'status' für jeden verfügbaren Slot.
    """
    event_type_uri = await get_event_type_uri()

    now = datetime.now(timezone.utc) + timedelta(minutes=5)
    start_time = now.strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    end_time = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{CALENDLY_BASE_URL}/event_type_available_times",
            headers=_headers(),
            params={
                "event_type": event_type_uri,
                "start_time": start_time,
                "end_time": end_time,
            },
        )
        resp.raise_for_status()
        slots = resp.json().get("collection", [])

    logger.info(f"Calendly: {len(slots)} verfügbare Slots in den nächsten {days_ahead} Tagen")

    # Nur Slots innerhalb der Bürozeiten (lokale Zeit) behalten
    tz = ZoneInfo(BOOKING_TIMEZONE)
    filtered = []
    for slot in slots:
        start_local = datetime.fromisoformat(slot["start_time"]).astimezone(tz)
        window = WEEKDAY_BOOKING_WINDOWS.get(start_local.weekday())
        if window is None:
            continue
        hour_min, hour_max = window
        if hour_min <= start_local.hour < hour_max:
            filtered.append(slot)

    logger.info("Calendly: %s Slots nach Bürozeiten-Filter", len(filtered))
    return filtered


async def format_available_slots(days_ahead: int = 5) -> str:
    """
    Gibt verfügbare Termine als lesbaren String zurück (für den Agent).
    """
    try:
        slots = await get_available_slots(days_ahead)
    except httpx.HTTPStatusError as e:
        logger.error(f"Calendly API Fehler: {e.response.status_code} - {e.response.text}")
        return "Calendly-Verfügbarkeit konnte nicht abgerufen werden."
    except Exception as e:
        logger.error(f"Calendly Fehler: {e}")
        return "Calendly-Verfügbarkeit konnte nicht abgerufen werden."

    if not slots:
        return "Keine freien Termine in den nächsten Tagen gefunden."

    # Gruppiere nach Tag (deutsche Zeitzone)
    tz = ZoneInfo(BOOKING_TIMEZONE)
    days: dict[str, list[str]] = {}
    for slot in slots:
        start = datetime.fromisoformat(slot["start_time"]).astimezone(tz)
        day_key = start.strftime("%A, %d. %B %Y")
        time_str = start.strftime("%H:%M")
        days.setdefault(day_key, []).append(time_str)

    lines = ["Verfügbare Termine:"]
    for day, times in days.items():
        times_str = ", ".join(times[:6])  # Max 6 Zeiten pro Tag zeigen
        if len(times) > 6:
            times_str += f" (+{len(times) - 6} weitere)"
        lines.append(f"  {day}: {times_str}")

    return "\n".join(lines)


async def create_scheduling_link() -> str | None:
    """
    Erstellt einen Einmal-Buchungslink für den Event Type.

    Returns:
        Booking-URL oder None bei Fehler.
    """
    event_type_uri = await get_event_type_uri()

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{CALENDLY_BASE_URL}/scheduling_links",
            headers=_headers(),
            json={
                "max_event_count": 1,
                "owner": event_type_uri,
                "owner_type": "EventType",
            },
        )
        resp.raise_for_status()
        booking_url = resp.json()["resource"]["booking_url"]

    logger.info(f"Calendly Buchungslink erstellt: {booking_url}")
    return booking_url
