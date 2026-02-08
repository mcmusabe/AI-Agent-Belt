"""
Google Calendar helper utilities.

Requires:
- google-api-python-client
- google-auth
- google-auth-oauthlib
"""
from typing import Any, Dict, List, Optional

from ..config import get_settings

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def _get_credentials():
    settings = get_settings()
    if not settings.google_client_id or not settings.google_client_secret or not settings.google_refresh_token:
        raise ValueError("Google OAuth is niet geconfigureerd (client_id/secret/refresh_token).")

    try:
        from google.oauth2.credentials import Credentials
    except ImportError as exc:
        raise RuntimeError("Google dependencies ontbreken. Installeer google-api-python-client en google-auth.") from exc

    return Credentials(
        None,
        refresh_token=settings.google_refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=SCOPES,
    )


def get_calendar_service():
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError("Google dependencies ontbreken. Installeer google-api-python-client.") from exc

    creds = _get_credentials()
    return build("calendar", "v3", credentials=creds)


def create_calendar_event(
    summary: str,
    start_iso: str,
    end_iso: str,
    timezone: str = "Europe/Amsterdam",
    description: Optional[str] = None,
    location: Optional[str] = None,
    attendees: Optional[List[str]] = None,
) -> Dict[str, Any]:
    settings = get_settings()
    service = get_calendar_service()

    event: Dict[str, Any] = {
        "summary": summary,
        "start": {"dateTime": start_iso, "timeZone": timezone},
        "end": {"dateTime": end_iso, "timeZone": timezone},
    }
    if description:
        event["description"] = description
    if location:
        event["location"] = location
    if attendees:
        event["attendees"] = [{"email": a} for a in attendees]

    calendar_id = settings.google_calendar_id or "primary"
    return service.events().insert(calendarId=calendar_id, body=event).execute()


def list_events(
    time_min: str,
    time_max: str,
    timezone: str = "Europe/Amsterdam",
    max_results: int = 50,
) -> List[Dict[str, Any]]:
    """
    Haal agenda-events op binnen een tijdsperiode.

    Args:
        time_min: Start ISO datetime (bijv. '2025-01-01T00:00:00+01:00')
        time_max: End ISO datetime
        timezone: IANA timezone
        max_results: Maximum aantal events

    Returns:
        Lijst van event dicts met summary, start, end, etc.
    """
    settings = get_settings()
    service = get_calendar_service()
    calendar_id = settings.google_calendar_id or "primary"

    result = (
        service.events()
        .list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime",
            timeZone=timezone,
        )
        .execute()
    )

    return result.get("items", [])


def get_free_slots(
    date_str: str,
    timezone: str = "Europe/Amsterdam",
    work_start: int = 9,
    work_end: int = 18,
    min_slot_minutes: int = 30,
) -> List[Dict[str, str]]:
    """
    Bereken vrije tijdsloten op een bepaalde dag.

    Args:
        date_str: Datum als 'YYYY-MM-DD'
        timezone: IANA timezone
        work_start: Begin werkdag (uur)
        work_end: Einde werkdag (uur)
        min_slot_minutes: Minimale slotgrootte in minuten

    Returns:
        Lijst van dicts met 'start' en 'end' als HH:MM strings
    """
    from datetime import datetime, timedelta
    import pytz

    tz = pytz.timezone(timezone)
    day = datetime.strptime(date_str, "%Y-%m-%d")
    day_start = tz.localize(day.replace(hour=work_start, minute=0, second=0))
    day_end = tz.localize(day.replace(hour=work_end, minute=0, second=0))

    events = list_events(
        time_min=day_start.isoformat(),
        time_max=day_end.isoformat(),
        timezone=timezone,
    )

    # Bouw lijst van bezette periodes
    busy: List[tuple] = []
    for ev in events:
        start_raw = ev.get("start", {})
        end_raw = ev.get("end", {})
        # dateTime voor timed events, date voor all-day
        s = start_raw.get("dateTime") or start_raw.get("date")
        e = end_raw.get("dateTime") or end_raw.get("date")
        if not s or not e:
            continue
        try:
            ev_start = datetime.fromisoformat(s)
            ev_end = datetime.fromisoformat(e)
            if ev_start.tzinfo is None:
                ev_start = tz.localize(ev_start)
            if ev_end.tzinfo is None:
                ev_end = tz.localize(ev_end)
            busy.append((ev_start, ev_end))
        except (ValueError, TypeError):
            continue

    # Sorteer op start
    busy.sort(key=lambda x: x[0])

    # Vind vrije slots
    free_slots: List[Dict[str, str]] = []
    cursor = day_start
    min_delta = timedelta(minutes=min_slot_minutes)

    for b_start, b_end in busy:
        if b_start > cursor:
            gap = b_start - cursor
            if gap >= min_delta:
                free_slots.append({
                    "start": cursor.strftime("%H:%M"),
                    "end": b_start.strftime("%H:%M"),
                })
        if b_end > cursor:
            cursor = b_end

    # Slot na laatste event tot einde werkdag
    if cursor < day_end:
        gap = day_end - cursor
        if gap >= min_delta:
            free_slots.append({
                "start": cursor.strftime("%H:%M"),
                "end": day_end.strftime("%H:%M"),
            })

    return free_slots
