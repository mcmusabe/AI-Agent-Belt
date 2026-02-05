"""
Gmail helper utilities.

Requires:
- google-api-python-client
- google-auth
- google-auth-oauthlib
"""
import base64
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, Optional

from ..config import get_settings

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


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


def get_gmail_service():
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError("Google dependencies ontbreken. Installeer google-api-python-client.") from exc

    creds = _get_credentials()
    return build("gmail", "v1", credentials=creds)


def send_email(
    to_email: str,
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
    from_email: Optional[str] = None
) -> Dict[str, Any]:
    settings = get_settings()
    service = get_gmail_service()

    if body_html:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body_text, "plain"))
        msg.attach(MIMEText(body_html, "html"))
    else:
        msg = MIMEText(body_text)

    msg["To"] = to_email
    msg["Subject"] = subject
    if from_email or settings.gmail_from_email:
        msg["From"] = from_email or settings.gmail_from_email

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    return service.users().messages().send(userId="me", body={"raw": raw}).execute()
