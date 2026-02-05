"""
Connect Smart - Main Application

Een autonoom AI-systeem dat:
- Opdrachten ontvangt via WhatsApp of API
- Websites kan bedienen (reserveren, boeken)
- Kan bellen naar restaurants/bedrijven
- Taken plant en uitvoert

Start met: uvicorn src.main:app --reload
"""
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import asyncio
import logging

from .config import get_settings
from .orchestrator.graph import process_request
from .channels.whatsapp import router as whatsapp_router
from .channels.telegram import start_telegram_bot
from .agents.browser import BrowserAgent, quick_browser_task
from .agents.voice import VoiceAgent
from .agents.planner import PlannerAgent


# Initialiseer FastAPI app
app = FastAPI(
    title="Connect Smart",
    description="Autonoom AI-systeem voor reserveringen en taken",
    version="1.0.0"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Voeg WhatsApp router toe
app.include_router(whatsapp_router)


# === REQUEST MODELS ===

class TaskRequest(BaseModel):
    """Algemeen taakverzoek"""
    task: str
    user_id: Optional[str] = "api_user"


class ReservationRequest(BaseModel):
    """Reserveringsverzoek"""
    venue_name: str
    date: str
    time: str
    party_size: int
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    special_requests: Optional[str] = None
    use_phone: bool = False  # Of we moeten bellen ipv online


class CallRequest(BaseModel):
    """Telefoonverzoek"""
    phone_number: str
    restaurant_name: str
    date: str
    time: str
    party_size: int
    customer_name: str
    special_requests: Optional[str] = None


# === ENDPOINTS ===

@app.get("/")
async def root():
    """Health check en info"""
    return {
        "name": "Connect Smart",
        "version": "1.0.0",
        "status": "running",
        "telegram_bot": "Connect Smart (@ai_agent_belt_bot)",
        "endpoints": {
            "POST /task": "Voer een algemene taak uit",
            "POST /reserve": "Maak een reservering",
            "POST /call": "Bel een restaurant",
            "GET /whatsapp/webhook": "WhatsApp verificatie",
            "POST /whatsapp/webhook": "WhatsApp berichten"
        }
    }


@app.post("/task")
async def execute_task(request: TaskRequest):
    """
    Voer een algemene taak uit via de agent orchestrator.
    
    Dit is de main endpoint voor alle soorten taken.
    De AI bepaalt zelf welke agents nodig zijn.
    """
    try:
        result = await process_request(
            user_message=request.task,
            user_id=request.user_id
        )
        return {
            "success": True,
            "result": result
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/reserve")
async def make_reservation(request: ReservationRequest):
    """
    Maak een reservering.
    
    Probeert eerst online te reserveren.
    Als use_phone=True of online mislukt, belt de AI.
    """
    browser_agent = BrowserAgent()
    voice_agent = VoiceAgent()
    
    try:
        if not request.use_phone:
            # Probeer online
            result = await browser_agent.make_reservation(
                venue_name=request.venue_name,
                date=request.date,
                time=request.time,
                party_size=request.party_size,
                name=request.name,
                email=request.email,
                phone=request.phone,
                special_requests=request.special_requests
            )
            
            if result.get("success"):
                return {"success": True, "method": "online", "result": result}
        
        # Fallback naar telefoon (of direct als use_phone=True)
        if request.phone:
            # We hebben een telefoonnummer van de venue nodig
            return {
                "success": False,
                "message": "Telefonisch reserveren vereist het telefoonnummer van het restaurant",
                "suggestion": "Geef het telefoonnummer mee in een /call request"
            }
        
        return {
            "success": False,
            "message": "Online reservering niet gelukt en geen telefoonnummer beschikbaar"
        }
        
    finally:
        await browser_agent.close()


@app.post("/call")
async def call_restaurant(request: CallRequest):
    """
    Bel een restaurant voor een reservering.
    
    De AI voert het gesprek in het Nederlands.
    """
    voice_agent = VoiceAgent()
    
    result = await voice_agent.call_restaurant_for_reservation(
        restaurant_name=request.restaurant_name,
        phone_number=request.phone_number,
        date=request.date,
        time=request.time,
        party_size=request.party_size,
        customer_name=request.customer_name,
        special_requests=request.special_requests,
        be_persistent=True
    )
    
    return result


@app.get("/call/{call_id}/status")
async def get_call_status(call_id: str):
    """Haal de status van een telefoongesprek op"""
    voice_agent = VoiceAgent()
    return await voice_agent.get_call_status(call_id)


@app.get("/call/{call_id}/transcript")
async def get_call_transcript(call_id: str):
    """Haal de transcriptie van een gesprek op"""
    voice_agent = VoiceAgent()
    return await voice_agent.get_call_transcript(call_id)


@app.get("/call/{call_id}/wait")
async def wait_for_call_result(call_id: str, max_wait: int = 180):
    """
    Wacht tot een call klaar is en retourneer het geanalyseerde resultaat.

    Args:
        call_id: ID van de call
        max_wait: Maximale wachttijd in seconden (default 180)
    """
    voice_agent = VoiceAgent()
    return await voice_agent.wait_and_analyze_call(call_id, max_wait)


# === VAPI WEBHOOK ===

webhook_logger = logging.getLogger("vapi_webhook")

# In-memory store voor webhook events (voor demo; in productie: Redis/DB)
_webhook_events: dict = {}


@app.post("/vapi/webhook")
async def vapi_webhook(request: Request):
    """
    Webhook endpoint voor Vapi call events.

    Vapi stuurt events zoals:
    - call-started
    - call-ended
    - transcript-ready
    - speech-update

    Configureer in Vapi dashboard: https://jouw-server.com/vapi/webhook
    """
    try:
        payload = await request.json()
    except Exception:
        return {"status": "error", "message": "Invalid JSON"}

    event_type = payload.get("message", {}).get("type") or payload.get("type", "unknown")
    call_data = payload.get("message", {}).get("call") or payload.get("call", {})
    call_id = call_data.get("id") or payload.get("call_id", "unknown")

    webhook_logger.info(f"Vapi webhook: {event_type} for call {call_id}")

    # Store event
    if call_id not in _webhook_events:
        _webhook_events[call_id] = []

    _webhook_events[call_id].append({
        "type": event_type,
        "timestamp": datetime.utcnow().isoformat(),
        "data": payload
    })

    # Handle specifieke events
    if event_type == "call-ended":
        ended_reason = call_data.get("endedReason", "unknown")
        duration = call_data.get("duration", 0)
        webhook_logger.info(
            f"Call {call_id} ended: reason={ended_reason}, duration={duration}s"
        )

        # TODO: Stuur notificatie naar gebruiker via Telegram/WhatsApp

    elif event_type == "transcript":
        transcript = payload.get("message", {}).get("transcript", "")
        webhook_logger.debug(f"Transcript update for {call_id}: {transcript[:100]}...")

    elif event_type == "status-update":
        status = payload.get("message", {}).get("status", "unknown")
        webhook_logger.info(f"Call {call_id} status: {status}")

    return {"status": "received", "call_id": call_id, "event": event_type}


@app.get("/vapi/webhook/events/{call_id}")
async def get_webhook_events(call_id: str):
    """Haal webhook events op voor een specifieke call"""
    events = _webhook_events.get(call_id, [])
    return {
        "call_id": call_id,
        "event_count": len(events),
        "events": events
    }


@app.post("/browser/task")
async def browser_task(request: TaskRequest):
    """
    Voer een directe browser taak uit.
    
    Handig voor debugging en directe browser controle.
    """
    try:
        result = await quick_browser_task(request.task)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health_check():
    """Gedetailleerde health check"""
    settings = get_settings()
    
    checks = {
        "anthropic": bool(settings.anthropic_api_key),
        "vapi": bool(settings.vapi_private_key),
        "telegram": bool(settings.telegram_bot_token),
        "whatsapp": bool(settings.whatsapp_token),
        "supabase": bool(settings.supabase_url),
    }
    
    return {
        "status": "healthy" if all([checks["anthropic"], checks["vapi"]]) else "degraded",
        "checks": checks
    }


# === STARTUP/SHUTDOWN ===

# Telegram bot task reference
telegram_task = None

@app.on_event("startup")
async def startup():
    """Startup taken"""
    global telegram_task
    settings = get_settings()
    
    print("🚀 Connect Smart gestart!")
    print(f"   - Debug mode: {settings.debug}")
    print(f"   - Anthropic: {'✅' if settings.anthropic_api_key else '❌'}")
    print(f"   - Vapi: {'✅' if settings.vapi_private_key else '❌'}")
    print(f"   - Telegram: {'✅' if settings.telegram_bot_token else '❌'}")
    print(f"   - WhatsApp: {'✅' if settings.whatsapp_token else '⚠️ Niet geconfigureerd'}")
    
    # Start Telegram bot als achtergrondtaak
    if settings.telegram_bot_token:
        print("🤖 Telegram bot wordt gestart...")
        print("   📱 Bot: Connect Smart (@ai_agent_belt_bot)")
        telegram_task = asyncio.create_task(start_telegram_bot())


@app.on_event("shutdown")
async def shutdown():
    """Cleanup taken"""
    global telegram_task
    
    # Stop Telegram bot
    if telegram_task:
        telegram_task.cancel()
        try:
            await telegram_task
        except asyncio.CancelledError:
            pass
    
    print("👋 Connect Smart afgesloten")


# Voor directe uitvoering
if __name__ == "__main__":
    import uvicorn
    settings = get_settings()
    uvicorn.run(
        "src.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug
    )
