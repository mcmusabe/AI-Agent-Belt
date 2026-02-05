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
