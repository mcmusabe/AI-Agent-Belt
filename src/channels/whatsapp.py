"""
WhatsApp Channel - Integratie met WhatsApp Business API.

Ontvangt berichten via webhook en stuurt responses terug.
"""
import httpx
from typing import Optional, Dict, Any
from fastapi import APIRouter, Request, Response, HTTPException
from pydantic import BaseModel

from ..config import get_settings
from ..orchestrator.graph import process_request


router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])


class WhatsAppMessage(BaseModel):
    """Inkomend WhatsApp bericht"""
    from_number: str
    message_id: str
    text: str
    timestamp: str


async def send_whatsapp_message(to: str, message: str) -> Dict[str, Any]:
    """
    Stuur een WhatsApp bericht.
    
    Args:
        to: Telefoonnummer van de ontvanger
        message: Te sturen bericht
        
    Returns:
        API response
    """
    settings = get_settings()
    
    if not settings.whatsapp_token or not settings.whatsapp_phone_number_id:
        return {"success": False, "error": "WhatsApp niet geconfigureerd"}
    
    url = f"https://graph.facebook.com/v18.0/{settings.whatsapp_phone_number_id}/messages"
    
    headers = {
        "Authorization": f"Bearer {settings.whatsapp_token}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": message}
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.post(url, headers=headers, json=payload, timeout=30.0)
        
        if response.status_code == 200:
            return {"success": True, "data": response.json()}
        else:
            return {"success": False, "error": response.text}


def parse_webhook_message(data: Dict) -> Optional[WhatsAppMessage]:
    """Parse een inkomend webhook bericht"""
    try:
        entry = data.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])
        
        if not messages:
            return None
        
        msg = messages[0]
        
        # Alleen text berichten verwerken
        if msg.get("type") != "text":
            return None
        
        return WhatsAppMessage(
            from_number=msg.get("from", ""),
            message_id=msg.get("id", ""),
            text=msg.get("text", {}).get("body", ""),
            timestamp=msg.get("timestamp", "")
        )
    except Exception:
        return None


@router.get("/webhook")
async def verify_webhook(request: Request):
    """
    Webhook verificatie endpoint voor Meta.
    
    Meta stuurt een GET request met challenge om de webhook te verifiëren.
    """
    settings = get_settings()
    
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    
    if mode == "subscribe" and token == settings.whatsapp_verify_token:
        return Response(content=challenge, media_type="text/plain")
    
    raise HTTPException(status_code=403, detail="Verificatie mislukt")


@router.post("/webhook")
async def receive_webhook(request: Request):
    """
    Ontvang inkomende WhatsApp berichten.
    
    Dit is de main entry point voor berichten van gebruikers.
    """
    try:
        data = await request.json()
        
        # Parse het bericht
        message = parse_webhook_message(data)
        
        if not message:
            # Geen bericht om te verwerken (bijv. status update)
            return {"status": "ignored"}
        
        # Log het bericht
        print(f"📱 Bericht van {message.from_number}: {message.text}")
        
        # Verwerk via de agent orchestrator
        response_text = await process_request(
            user_message=message.text,
            user_id=message.from_number
        )
        
        # Stuur response terug
        await send_whatsapp_message(
            to=message.from_number,
            message=response_text
        )
        
        return {"status": "processed"}
        
    except Exception as e:
        print(f"❌ Webhook error: {e}")
        return {"status": "error", "message": str(e)}


# Handige test functie
async def test_whatsapp_connection() -> Dict[str, Any]:
    """Test de WhatsApp API connectie"""
    settings = get_settings()
    
    if not settings.whatsapp_token:
        return {
            "connected": False,
            "error": "WHATSAPP_TOKEN niet geconfigureerd"
        }
    
    url = f"https://graph.facebook.com/v18.0/{settings.whatsapp_phone_number_id}"
    headers = {"Authorization": f"Bearer {settings.whatsapp_token}"}
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=headers, timeout=10.0)
            if response.status_code == 200:
                return {"connected": True, "data": response.json()}
            else:
                return {"connected": False, "error": response.text}
        except Exception as e:
            return {"connected": False, "error": str(e)}
