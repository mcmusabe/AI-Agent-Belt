"""
SMS Tool - Verstuur SMS berichten via Twilio REST API.

Gebruikt httpx (al een dependency) in plaats van de twilio SDK.
"""
from typing import Any, Dict, Optional
import logging
import httpx

from ..config import get_settings

logger = logging.getLogger(__name__)


async def send_sms(
    to_number: str,
    body: str,
    from_number: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Verstuur een SMS via Twilio REST API.

    Args:
        to_number: Bestemmingsnummer (E.164 formaat, bijv. +31612345678)
        body: SMS tekst (max 1600 tekens)
        from_number: Afzendernummer (optioneel, valt terug op config)

    Returns:
        Dict met success status en Twilio message SID
    """
    settings = get_settings()

    if not settings.twilio_account_sid or not settings.twilio_auth_token:
        raise ValueError(
            "Twilio credentials niet geconfigureerd "
            "(TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN)"
        )

    sender = from_number or settings.twilio_sms_number
    messaging_service_sid = settings.twilio_messaging_service_sid

    if not sender and not messaging_service_sid:
        raise ValueError(
            "Geen SMS afzendernummer geconfigureerd "
            "(TWILIO_SMS_NUMBER of TWILIO_MESSAGING_SERVICE_SID)"
        )

    # Truncate naar Twilio limiet
    if len(body) > 1600:
        body = body[:1597] + "..."

    url = (
        f"https://api.twilio.com/2010-04-01/Accounts/"
        f"{settings.twilio_account_sid}/Messages.json"
    )

    payload: Dict[str, str] = {
        "To": to_number,
        "Body": body,
    }
    if messaging_service_sid:
        payload["MessagingServiceSid"] = messaging_service_sid
    else:
        payload["From"] = sender

    logger.info("📱 SMS versturen naar %s (%d tekens)", to_number, len(body))

    async with httpx.AsyncClient() as client:
        response = await client.post(
            url,
            data=payload,
            auth=(settings.twilio_account_sid, settings.twilio_auth_token),
            timeout=30.0,
        )

    if response.status_code == 201:
        data = response.json()
        logger.info("✅ SMS verstuurd: SID=%s status=%s", data.get("sid"), data.get("status"))
        return {
            "success": True,
            "message_sid": data.get("sid"),
            "to": to_number,
            "status": data.get("status"),
        }
    else:
        logger.error("❌ SMS mislukt: %s %s", response.status_code, response.text[:200])
        return {
            "success": False,
            "error": response.text,
            "status_code": response.status_code,
        }
