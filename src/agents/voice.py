"""
Voice Agent - Gebruikt Vapi.ai voor telefoongesprekken.

Kan:
- Uitgaande telefoongesprekken voeren
- Restaurants bellen voor reserveringen
- Onderhandelen en alternatieven vragen
- Gesprekken in het Nederlands voeren
"""
import httpx
from typing import Optional, Dict, Any
from ..config import get_settings


class VoiceAgent:
    """Agent die telefoongesprekken kan voeren via Vapi.ai"""
    
    VAPI_BASE_URL = "https://api.vapi.ai"
    
    def __init__(self):
        self.settings = get_settings()
        self.headers = {
            "Authorization": f"Bearer {self.settings.vapi_private_key}",
            "Content-Type": "application/json"
        }
    
    async def create_call(
        self,
        phone_number: str,
        first_message: str,
        system_prompt: str,
        voice_id: str = "dutch",  # Nederlands
        max_duration_seconds: int = 300,
        phone_number_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Start een uitgaand telefoongesprek.
        
        Args:
            phone_number: Telefoonnummer om te bellen (internationaal formaat)
            first_message: Eerste bericht dat de AI zegt
            system_prompt: Instructies voor de AI
            voice_id: Voice ID voor de stem
            max_duration_seconds: Max gespreksduur
            phone_number_id: Vapi phone number ID (indien je een nummer hebt gekocht)
            
        Returns:
            dict met call details
        """
        payload = {
            "assistant": {
                "firstMessage": first_message,
                "model": {
                    "provider": "anthropic",
                    "model": "claude-sonnet-4-20250514",
                    "messages": [
                        {
                            "role": "system",
                            "content": system_prompt
                        }
                    ]
                },
                "voice": {
                    "provider": "11labs",
                    "voiceId": voice_id
                },
                "maxDurationSeconds": max_duration_seconds,
                "endCallMessage": "Bedankt voor uw tijd. Tot ziens!",
                "silenceTimeoutSeconds": 30,
            },
            "customer": {
                "number": phone_number
            }
        }
        
        # Als we een Vapi telefoonnummer hebben
        if phone_number_id:
            payload["phoneNumberId"] = phone_number_id
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.VAPI_BASE_URL}/call/phone",
                headers=self.headers,
                json=payload,
                timeout=30.0
            )
            
            if response.status_code == 201:
                return {
                    "success": True,
                    "call": response.json()
                }
            else:
                return {
                    "success": False,
                    "error": response.text,
                    "status_code": response.status_code
                }
    
    async def call_restaurant_for_reservation(
        self,
        restaurant_name: str,
        phone_number: str,
        date: str,
        time: str,
        party_size: int,
        customer_name: str,
        special_requests: Optional[str] = None,
        be_persistent: bool = True
    ) -> Dict[str, Any]:
        """
        Bel een restaurant om een reservering te maken.
        
        Args:
            restaurant_name: Naam van het restaurant
            phone_number: Telefoonnummer van het restaurant
            date: Gewenste datum
            time: Gewenste tijd
            party_size: Aantal personen
            customer_name: Naam voor de reservering
            special_requests: Speciale verzoeken
            be_persistent: Of de AI moet aandringen bij "vol"
            
        Returns:
            dict met resultaat van het gesprek
        """
        persistence_instructions = ""
        if be_persistent:
            persistence_instructions = """
            Als het restaurant zegt dat ze vol zitten:
            - Vraag of er later op de avond nog plek is
            - Vraag of er aan de bar gezeten kan worden
            - Vraag of er een wachtlijst is
            - Vraag of er op een andere dag wel plek is
            Wees beleefd maar vasthoudend.
            """
        
        system_prompt = f"""
        Je bent een persoonlijke assistent die belt namens {customer_name}.
        Je spreekt vloeiend Nederlands met een vriendelijke, professionele toon.
        
        DOEL: Reserveer een tafel bij {restaurant_name}
        
        DETAILS RESERVERING:
        - Datum: {date}
        - Tijd: {time}
        - Aantal personen: {party_size}
        - Naam: {customer_name}
        {f"- Speciale verzoeken: {special_requests}" if special_requests else ""}
        
        {persistence_instructions}
        
        BELANGRIJK:
        - Wees beleefd en professioneel
        - Bevestig de details aan het eind van het gesprek
        - Vraag om een bevestigingsnummer indien beschikbaar
        - Als de reservering lukt, bedank het restaurant
        - Als het niet lukt, vraag naar alternatieven
        """
        
        first_message = f"Goedendag, ik bel namens {customer_name}. Ik zou graag een tafel willen reserveren voor {party_size} personen op {date} om {time}. Is dat mogelijk?"
        
        return await self.create_call(
            phone_number=phone_number,
            first_message=first_message,
            system_prompt=system_prompt
        )
    
    async def get_call_status(self, call_id: str) -> Dict[str, Any]:
        """
        Haal de status van een gesprek op.
        
        Args:
            call_id: ID van het gesprek
            
        Returns:
            dict met call status
        """
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.VAPI_BASE_URL}/call/{call_id}",
                headers=self.headers,
                timeout=30.0
            )
            
            if response.status_code == 200:
                return {
                    "success": True,
                    "call": response.json()
                }
            else:
                return {
                    "success": False,
                    "error": response.text
                }
    
    async def get_call_transcript(self, call_id: str) -> Dict[str, Any]:
        """
        Haal de transcriptie van een gesprek op.
        
        Args:
            call_id: ID van het gesprek
            
        Returns:
            dict met transcript
        """
        call_status = await self.get_call_status(call_id)
        
        if call_status["success"]:
            call_data = call_status["call"]
            return {
                "success": True,
                "transcript": call_data.get("transcript", ""),
                "summary": call_data.get("summary", ""),
                "status": call_data.get("status", ""),
                "duration": call_data.get("duration", 0)
            }
        
        return call_status
    
    async def list_phone_numbers(self) -> Dict[str, Any]:
        """
        Lijst alle beschikbare telefoonnummers op.
        
        Returns:
            dict met phone numbers
        """
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.VAPI_BASE_URL}/phone-number",
                headers=self.headers,
                timeout=30.0
            )
            
            if response.status_code == 200:
                return {
                    "success": True,
                    "phone_numbers": response.json()
                }
            else:
                return {
                    "success": False,
                    "error": response.text
                }


# Convenience functie
async def quick_restaurant_call(
    restaurant_name: str,
    phone_number: str,
    date: str,
    time: str,
    party_size: int,
    customer_name: str
) -> Dict[str, Any]:
    """Snel een restaurant bellen voor reservering"""
    agent = VoiceAgent()
    return await agent.call_restaurant_for_reservation(
        restaurant_name=restaurant_name,
        phone_number=phone_number,
        date=date,
        time=time,
        party_size=party_size,
        customer_name=customer_name
    )
