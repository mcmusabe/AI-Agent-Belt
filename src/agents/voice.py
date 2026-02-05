"""
Voice Agent - Gebruikt Vapi.ai voor telefoongesprekken.

Kan:
- Uitgaande telefoongesprekken voeren
- Restaurants bellen voor reserveringen
- Onderhandelen en alternatieven vragen
- Gesprekken in het Nederlands voeren

Verbeteringen:
- Retry logic met exponential backoff
- Natuurlijke, menselijke conversatie
- Real-time call tracking
- Transcript analyse voor resultaat extractie
"""
import logging
import httpx
import asyncio
import re
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime
from ..config import get_settings

logger = logging.getLogger(__name__)

# Vapi endedReason -> korte Nederlandse uitleg (uit docs: Call end reasons)
# Zodat het nooit meer "gebeurt" zonder uitleg: gebruiker ziet altijd waarom een call stopt.
ENDED_REASON_MESSAGES = {
    "customer-did-not-answer": "Er werd niet opgenomen.",
    "customer-ended-call": "De andere partij heeft opgehangen.",
    "customer-busy": "De lijn was bezet.",
    "twilio-failed-to-connect-call": "Twilio kon geen verbinding maken. Controleer je Vapi/Twilio-nummer.",
    "vonage-failed-to-connect-call": "Vonage kon geen verbinding maken.",
    "assistant-error": "Er ging iets mis in de assistent. Controleer model/voice in Vapi.",
    "assistant-join-timed-out": "De assistent kon niet op tijd meedoen. Probeer opnieuw.",
    "assistant-not-provided": "Geen assistent geconfigureerd.",
    "call.start.error-get-phone-number": "Vapi kon het beller-nummer niet ophalen. Controleer VAPI_PHONE_NUMBER_ID.",
    "call.start.error-get-assistant": "Assistent-configuratie kon niet worden geladen.",
    "call.start.error-get-customer": "Klantnummer ongeldig of ontbreekt.",
    "call.start.error-get-resources-validation": "Validatiefout bij resources. Controleer Vapi-dashboard.",
    "call-start-error-neither-assistant-nor-server-set": "Geen assistent geconfigureerd voor de call.",
    "silence-timed-out": "Gesprek beëindigd wegens te lange stilte.",
    "voicemail": "De call ging naar voicemail.",
    "exceeded-max-duration": "Maximale gespreksduur bereikt.",
    "phone-call-provider-closed-websocket": "Verbinding met de provider werd verbroken.",
    "pipeline-no-available-llm-model": "Geen LLM beschikbaar. Controleer Anthropic-credentials in Vapi.",
    "unknown-error": "Onbekende fout. Bekijk Vapi Dashboard → Call Logs voor dit gesprek.",
}


def _ended_reason_to_message(reason: Optional[str]) -> str:
    if not reason:
        return "Onbekende reden."
    return ENDED_REASON_MESSAGES.get(reason) or f"Reden: {reason}"


# Retry configuratie
RETRY_CONFIG = {
    "max_retries": 3,
    "base_delay": 2.0,  # seconden
    "max_delay": 10.0,
    "retryable_errors": [
        "twilio-failed-to-connect-call",
        "vonage-failed-to-connect-call",
        "assistant-join-timed-out",
        "phone-call-provider-closed-websocket",
        "unknown-error",
    ],
    "retryable_http_codes": [429, 500, 502, 503, 504],
}


class CallResult:
    """Gestructureerd resultaat van een call analyse"""
    def __init__(
        self,
        success: bool,
        reservation_confirmed: bool = False,
        confirmation_number: Optional[str] = None,
        alternative_offered: Optional[str] = None,
        next_steps: Optional[str] = None,
        summary: str = "",
        raw_transcript: str = ""
    ):
        self.success = success
        self.reservation_confirmed = reservation_confirmed
        self.confirmation_number = confirmation_number
        self.alternative_offered = alternative_offered
        self.next_steps = next_steps
        self.summary = summary
        self.raw_transcript = raw_transcript

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "reservation_confirmed": self.reservation_confirmed,
            "confirmation_number": self.confirmation_number,
            "alternative_offered": self.alternative_offered,
            "next_steps": self.next_steps,
            "summary": self.summary,
        }


class VoiceAgent:
    """Agent die telefoongesprekken kan voeren via Vapi.ai

    Features:
    - Natuurlijke Nederlandse conversatie
    - Retry logic met exponential backoff
    - Real-time status tracking
    - Automatische transcript analyse
    """

    VAPI_BASE_URL = "https://api.vapi.ai"

    # ElevenLabs stemmen - fallback als ELEVENLABS_VOICE_ID niet gezet is
    ELEVENLABS_VOICES = {
        "rachel": "21m00Tcm4TlvDq8ikWAM",      # Vrouwelijk, warm (default)
        "adam": "pNInz6obpgDQGcFmaJgB",        # Mannelijk, vriendelijk
        "josh": "TxGEqnHWrfWFTfGW9XjX",        # Mannelijk, casual
        "bella": "EXAVITQu4vr4xnSDxMaL",       # Vrouwelijk, jong
    }
    DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # rachel

    # Fallback: Azure Neural Nederlandse stemmen
    AZURE_VOICES = {
        "maarten": "nl-NL-MaartenNeural",
        "colette": "nl-NL-ColetteNeural",
        "fenna": "nl-NL-FennaNeural",
    }

    def __init__(self):
        self.settings = get_settings()
        self.headers = {
            "Authorization": f"Bearer {self.settings.vapi_private_key}",
            "Content-Type": "application/json"
        }
        self._active_calls: Dict[str, Dict[str, Any]] = {}  # Track active calls
    
    async def create_call(
        self,
        phone_number: str,
        first_message: str,
        system_prompt: str,
        voice_id: str = "rachel",  # ElevenLabs voice key of direct ID
        max_duration_seconds: int = 300,
        phone_number_id: Optional[str] = None,
        enable_retry: bool = True,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Start een uitgaand telefoongesprek met retry logic.

        Args:
            phone_number: Telefoonnummer om te bellen (internationaal formaat)
            first_message: Eerste bericht dat de AI zegt
            system_prompt: Instructies voor de AI
            voice_id: Voice key (rachel, adam, josh, bella) of direct ElevenLabs ID
            max_duration_seconds: Max gespreksduur
            phone_number_id: Vapi phone number ID
            enable_retry: Of retry logic actief is bij tijdelijke fouten
            metadata: Extra metadata om op te slaan bij de call

        Returns:
            dict met call details
        """
        # Validatie
        validation_error = self._validate_call_params(phone_number, first_message, system_prompt)
        if validation_error:
            return validation_error

        phone_clean = self._clean_phone_number(phone_number)
        phone_number_id = phone_number_id or self.settings.vapi_phone_number_id

        if not phone_number_id:
            return {
                "success": False,
                "error": "Geen Vapi telefoonnummer geconfigureerd (VAPI_PHONE_NUMBER_ID).",
                "status_code": 400
            }

        # Voice: voorkeur voor geconfigureerde ElevenLabs-stem (bijv. Nederlandse accent uit Voice Library)
        effective_voice_id = (
            self.settings.elevenlabs_voice_id.strip()
            or self.ELEVENLABS_VOICES.get(voice_id, voice_id)
        )

        # Als er een Vapi assistant is geconfigureerd, gebruik die.
        if self.settings.vapi_assistant_id:
            transcriber_config: Dict[str, Any] = {
                "provider": "deepgram",
                "language": "nl",
                "model": "nova-2",
                "keywords": [
                    "broodje", "cola", "biertje", "koffie", "thee", "water",
                    "frikandel", "kroket", "patat", "pizza", "salade", "soep",
                    "Musabbe", "Musabi", "Connect", "Sophie",
                ],
                "keyterm": [
                    "hete kip", "broodje hete kip", "een cola", "een biertje",
                    "lunch bestelling", "wat wil je drinken",
                ],
            }
            overrides: Dict[str, Any] = {
                "firstMessage": first_message,
                "model": {
                    "provider": "anthropic",
                    "model": "claude-sonnet-4-20250514",
                    "messages": [{"role": "system", "content": system_prompt}],
                    "temperature": 0.6,
                },
                "transcriber": transcriber_config,
                "responseDelaySeconds": 0.35,
                "silenceTimeoutSeconds": 10,
                "llmRequestDelaySeconds": 0.15,
            }
            if effective_voice_id:
                overrides["voice"] = {
                    "provider": "11labs",
                    "voiceId": effective_voice_id,
                }
            payload: Dict[str, Any] = {
                "assistantId": self.settings.vapi_assistant_id,
                "assistantOverrides": overrides,
                "customer": {"number": phone_clean},
                "phoneNumberId": phone_number_id,
            }
        else:
            payload = self._build_call_payload(
                phone_clean=phone_clean,
                first_message=first_message,
                system_prompt=system_prompt,
                voice_id=effective_voice_id,
                max_duration_seconds=max_duration_seconds,
                phone_number_id=phone_number_id,
            )

        # Execute met retry logic
        if enable_retry:
            return await self._execute_call_with_retry(payload, phone_number, metadata)
        else:
            return await self._execute_call(payload, phone_number, metadata)

    def _validate_call_params(
        self, phone_number: str, first_message: str, system_prompt: str
    ) -> Optional[Dict[str, Any]]:
        """Valideer call parameters vooraf"""
        phone_clean = self._clean_phone_number(phone_number)

        if not phone_clean or len(phone_clean) < 10:
            return {
                "success": False,
                "error": "Ongeldig telefoonnummer. Gebruik internationaal formaat, bijv. +31612345678.",
                "status_code": 400
            }
        if not phone_clean.startswith("+"):
            return {
                "success": False,
                "error": "Telefoonnummer moet met + beginnen (E.164), bijv. +31612345678.",
                "status_code": 400
            }
        if not first_message or not first_message.strip():
            return {
                "success": False,
                "error": "firstMessage is verplicht voor de call.",
                "status_code": 400
            }
        if not system_prompt or not system_prompt.strip():
            return {
                "success": False,
                "error": "system_prompt is verplicht voor de call.",
                "status_code": 400
            }
        return None

    def _clean_phone_number(self, phone_number: str) -> str:
        """Normaliseer telefoonnummer naar E.164"""
        return phone_number.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")

    def _build_call_payload(
        self,
        phone_clean: str,
        first_message: str,
        system_prompt: str,
        voice_id: str,
        max_duration_seconds: int,
        phone_number_id: str
    ) -> Dict[str, Any]:
        """Bouw geoptimaliseerde call payload voor natuurlijke gesprekken"""

        resolved_id = (voice_id or "").strip() or (self.settings.elevenlabs_voice_id or "").strip() or self.DEFAULT_VOICE_ID
        voice_config: Dict[str, Any] = {
            "provider": "11labs",
            "voiceId": resolved_id,
        }

        return {
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
                    ],
                    "temperature": 0.6,  # Iets lager voor consistentere responses
                },
                "voice": voice_config,
                # Snellere timing = natuurlijker gesprek (niet te traag)
                "silenceTimeoutSeconds": 10,
                "responseDelaySeconds": 0.35,
                "llmRequestDelaySeconds": 0.15,
                "numWordsToInterruptAssistant": 3,
                "maxDurationSeconds": max_duration_seconds,
                "endCallMessage": "Oké, bedankt! Doei!",
                "endCallPhrases": ["doei", "dag", "tot ziens", "bedankt", "dankjewel", "fijne dag"],
                "backgroundSound": "off",
                # Transcriptie: Nederlands + woordboost voor broodje, cola, biertje, etc.
                "transcriber": {
                    "provider": "deepgram",
                    "language": "nl",
                    "model": "nova-2",
                    "keywords": [
                        "broodje", "cola", "biertje", "koffie", "thee", "water",
                        "frikandel", "kroket", "patat", "pizza", "salade", "soep",
                    ],
                    "keyterm": ["hete kip", "broodje hete kip", "een cola", "een biertje", "lunch bestelling"],
                },
            },
            "customer": {
                "number": phone_clean
            },
            "phoneNumberId": phone_number_id
        }

    async def _execute_call(
        self,
        payload: Dict[str, Any],
        original_phone: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Execute single call attempt"""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.VAPI_BASE_URL}/call/phone",
                headers=self.headers,
                json=payload,
                timeout=30.0
            )

            if response.status_code == 201:
                call_data = response.json()
                call_id = call_data.get("id")

                # Track active call
                self._active_calls[call_id] = {
                    "phone_number": original_phone,
                    "started_at": datetime.utcnow().isoformat(),
                    "metadata": metadata or {},
                }

                logger.info("Vapi call created: id=%s to=%s", call_id, original_phone)
                return {
                    "success": True,
                    "call": call_data,
                    "call_id": call_id
                }
            else:
                return self._handle_call_error(response)

    async def _execute_call_with_retry(
        self,
        payload: Dict[str, Any],
        original_phone: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Execute call met exponential backoff retry"""
        max_retries = RETRY_CONFIG["max_retries"]
        base_delay = RETRY_CONFIG["base_delay"]
        max_delay = RETRY_CONFIG["max_delay"]

        last_error = None

        for attempt in range(max_retries + 1):
            if attempt > 0:
                # Exponential backoff: 2s, 4s, 8s (capped at max_delay)
                delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                logger.info(f"Retry attempt {attempt}/{max_retries} na {delay}s wachten...")
                await asyncio.sleep(delay)

            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        f"{self.VAPI_BASE_URL}/call/phone",
                        headers=self.headers,
                        json=payload,
                        timeout=30.0
                    )

                    if response.status_code == 201:
                        call_data = response.json()
                        call_id = call_data.get("id")

                        self._active_calls[call_id] = {
                            "phone_number": original_phone,
                            "started_at": datetime.utcnow().isoformat(),
                            "attempt": attempt + 1,
                            "metadata": metadata or {},
                        }

                        logger.info(
                            "Vapi call created: id=%s to=%s (attempt %d)",
                            call_id, original_phone, attempt + 1
                        )
                        return {
                            "success": True,
                            "call": call_data,
                            "call_id": call_id,
                            "attempts": attempt + 1
                        }

                    # Check if retryable HTTP error
                    if response.status_code in RETRY_CONFIG["retryable_http_codes"]:
                        last_error = self._handle_call_error(response)
                        logger.warning(
                            f"Retryable HTTP error {response.status_code}, attempt {attempt + 1}"
                        )
                        continue

                    # Non-retryable error
                    return self._handle_call_error(response)

            except httpx.TimeoutException:
                last_error = {
                    "success": False,
                    "error": "Request timeout - Vapi niet bereikbaar",
                    "status_code": 408
                }
                logger.warning(f"Timeout op attempt {attempt + 1}")
                continue

            except httpx.RequestError as e:
                last_error = {
                    "success": False,
                    "error": f"Netwerkfout: {str(e)}",
                    "status_code": 0
                }
                logger.warning(f"Request error op attempt {attempt + 1}: {e}")
                continue

        # All retries exhausted
        logger.error(f"Alle {max_retries + 1} pogingen gefaald voor {original_phone}")
        return {
            **(last_error or {"success": False, "error": "Onbekende fout"}),
            "attempts": max_retries + 1,
            "message_nl": f"Kon geen verbinding maken na {max_retries + 1} pogingen."
        }

    def _handle_call_error(self, response: httpx.Response) -> Dict[str, Any]:
        """Parse en format call error response"""
        err_text = response.text
        try:
            err_json = response.json()
            err_text = err_json.get("message", err_json.get("error", err_text))
        except Exception:
            pass

        logger.warning("Vapi call failed: status=%s body=%s", response.status_code, response.text[:500])
        return {
            "success": False,
            "error": err_text or f"HTTP {response.status_code}",
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
        be_persistent: bool = True,
        voice_id: str = "rachel",
        wait_for_result: bool = False,
        max_wait_seconds: int = 180
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
            voice_id: Stem (rachel, adam, josh, bella)
            wait_for_result: Wacht tot call klaar is en analyseer resultaat
            max_wait_seconds: Max wachttijd voor resultaat

        Returns:
            dict met resultaat van het gesprek
        """
        # Bouw natuurlijke system prompt
        system_prompt = self._build_restaurant_prompt(
            restaurant_name=restaurant_name,
            date=date,
            time=time,
            party_size=party_size,
            customer_name=customer_name,
            special_requests=special_requests,
            be_persistent=be_persistent
        )

        # Natuurlijke opening - variatie voor realisme
        first_message = self._build_natural_opening(
            customer_name=customer_name,
            party_size=party_size,
            date=date,
            time=time
        )

        result = await self.create_call(
            phone_number=phone_number,
            first_message=first_message,
            system_prompt=system_prompt,
            voice_id=voice_id,
            metadata={
                "type": "restaurant_reservation",
                "restaurant": restaurant_name,
                "date": date,
                "time": time,
                "party_size": party_size,
                "customer_name": customer_name,
            }
        )

        # Optioneel: wacht op resultaat en analyseer
        if wait_for_result and result.get("success"):
            call_id = result.get("call_id")
            analyzed = await self.wait_and_analyze_call(call_id, max_wait_seconds)
            result["analysis"] = analyzed

        return result

    def _build_restaurant_prompt(
        self,
        restaurant_name: str,
        date: str,
        time: str,
        party_size: int,
        customer_name: str,
        special_requests: Optional[str],
        be_persistent: bool
    ) -> str:
        """Bouw een duidelijke, Nederlandse system prompt"""

        persistence_block = ""
        if be_persistent:
            persistence_block = """
ALS ZE VOL ZITTEN:
- Vraag vriendelijk of een andere tijd wel kan
- Vraag of er een wachtlijst is
- Geef niet direct op, maar wees niet pusherig
"""

        special_block = ""
        if special_requests:
            special_block = f"\nSPECIALE WENSEN: {special_requests}\n"

        return f"""Je bent een vriendelijke medewerker die belt om een reservering te maken.
Je belt namens {customer_name}.

BELANGRIJKE REGELS:
1. Spreek duidelijk en rustig Nederlands
2. Herhaal jezelf NOOIT - als iemand je niet verstaat, zeg het anders
3. Luister goed naar wat de ander zegt voordat je reageert
4. Geef korte, directe antwoorden
5. Stel jezelf NIET voor als AI of assistent of robot

HOE JE PRAAT:
- Normaal, vriendelijk Nederlands
- Korte zinnen, geen lange verhalen
- Zeg "hallo" of "goedendag", niet "hey"
- Als je iets niet verstaat: "Sorry, kunt u dat herhalen?"

DE RESERVERING:
- Restaurant: {restaurant_name}
- Datum: {date}
- Tijd: {time}
- Aantal personen: {party_size}
- Naam: {customer_name}
{special_block}{persistence_block}
GESPREK AFRONDEN:
- Bevestig de reservering: "Dus {date} om {time} voor {party_size} personen op naam van {customer_name}"
- Bedank en hang op: "Prima, dank u wel. Tot ziens!"

ALS HET NIET LUKT:
- "Oké, dan proberen we het ergens anders. Bedankt!"
- Niet blijven doorgaan"""

    def _build_natural_opening(
        self,
        customer_name: str,
        party_size: int,
        date: str,
        time: str
    ) -> str:
        """Genereer een duidelijke openingszin"""
        # Duidelijke, professionele opening - geen variatie nodig
        return f"Goedendag, ik wil graag een tafel reserveren voor {party_size} personen op {date} om {time}. Is dat mogelijk?"
    
    async def get_call_status(self, call_id: str) -> Dict[str, Any]:
        """
        Haal de status van een gesprek op.

        Args:
            call_id: ID van het gesprek

        Returns:
            dict met call status en Nederlandse uitleg
        """
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.VAPI_BASE_URL}/call/{call_id}",
                headers=self.headers,
                timeout=30.0
            )

            if response.status_code == 200:
                call_data = response.json()
                status = call_data.get("status", "unknown")
                ended_reason = call_data.get("endedReason")

                # Bereken duur indien beschikbaar
                duration = self._calculate_duration(call_data)

                return {
                    "success": True,
                    "call": call_data,
                    "status": status,
                    "status_nl": self._status_to_dutch(status),
                    "ended_reason": ended_reason,
                    "ended_reason_nl": _ended_reason_to_message(ended_reason) if ended_reason else None,
                    "duration_seconds": duration,
                }
            else:
                return {
                    "success": False,
                    "error": response.text
                }

    def _status_to_dutch(self, status: str) -> str:
        """Vertaal call status naar Nederlands"""
        status_map = {
            "queued": "In de wachtrij",
            "ringing": "Gaat over",
            "in-progress": "Gesprek bezig",
            "forwarding": "Wordt doorgeschakeld",
            "ended": "Beëindigd",
        }
        return status_map.get(status, status)

    def _calculate_duration(self, call_data: Dict[str, Any]) -> Optional[int]:
        """Bereken gespreksduur uit call data"""
        # Probeer eerst directe duration
        if call_data.get("duration"):
            return int(call_data["duration"])

        # Anders bereken uit timestamps
        started = call_data.get("startedAt")
        ended = call_data.get("endedAt")

        if started and ended:
            try:
                start_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
                end_dt = datetime.fromisoformat(ended.replace("Z", "+00:00"))
                return max(0, int((end_dt - start_dt).total_seconds()))
            except Exception:
                pass

        return None
    
    async def get_call_transcript(self, call_id: str) -> Dict[str, Any]:
        """
        Haal de transcriptie en eindstatus van een gesprek op.
        Inclusief endedReason zodat we altijd kunnen uitleggen waarom een call stopte.
        """
        call_status = await self.get_call_status(call_id)

        if not call_status["success"]:
            return call_status

        call_data = call_status["call"]
        status = call_data.get("status", "")
        ended_reason = call_data.get("endedReason") or ""

        # Transcript/summary kunnen in artifact of top-level zitten
        artifact = call_data.get("artifact") or {}
        transcript = call_data.get("transcript") or artifact.get("transcript", "")
        summary = call_data.get("summary") or (artifact.get("summary") if isinstance(artifact.get("summary"), str) else "")

        # Duur berekenen
        duration = self._calculate_duration(call_data) or 0

        return {
            "success": True,
            "transcript": transcript or "",
            "summary": summary or "",
            "status": status,
            "status_nl": self._status_to_dutch(status),
            "duration": duration,
            "endedReason": ended_reason,
            "endedReason_nl": _ended_reason_to_message(ended_reason) if ended_reason else None,
        }

    async def wait_for_call_completion(
        self,
        call_id: str,
        max_wait_seconds: int = 180,
        poll_interval: float = 3.0
    ) -> Dict[str, Any]:
        """
        Wacht tot een call is afgerond.

        Args:
            call_id: ID van de call
            max_wait_seconds: Maximale wachttijd
            poll_interval: Tijd tussen status checks

        Returns:
            Final call status met transcript
        """
        start_time = datetime.utcnow()
        last_status = "unknown"

        while True:
            elapsed = (datetime.utcnow() - start_time).total_seconds()
            if elapsed > max_wait_seconds:
                logger.warning(f"Timeout wachten op call {call_id} na {max_wait_seconds}s")
                return {
                    "success": False,
                    "error": f"Timeout na {max_wait_seconds} seconden",
                    "last_status": last_status
                }

            status_result = await self.get_call_status(call_id)
            if not status_result.get("success"):
                await asyncio.sleep(poll_interval)
                continue

            last_status = status_result.get("status", "unknown")

            # Check of call klaar is
            if last_status == "ended":
                logger.info(f"Call {call_id} beëindigd na {elapsed:.1f}s")
                return await self.get_call_transcript(call_id)

            # Log progress
            if last_status == "in-progress":
                logger.debug(f"Call {call_id} nog bezig ({elapsed:.0f}s)")

            await asyncio.sleep(poll_interval)

    async def wait_and_analyze_call(
        self,
        call_id: str,
        max_wait_seconds: int = 180
    ) -> Dict[str, Any]:
        """
        Wacht op call completion en analyseer het resultaat.

        Returns:
            Dict met transcript + gestructureerde analyse
        """
        # Wacht tot call klaar
        result = await self.wait_for_call_completion(call_id, max_wait_seconds)

        if not result.get("success"):
            return result

        # Analyseer transcript
        transcript = result.get("transcript", "")
        analysis = self.analyze_transcript(transcript)

        return {
            **result,
            "analysis": analysis.to_dict() if isinstance(analysis, CallResult) else analysis
        }

    def analyze_transcript(self, transcript: str) -> CallResult:
        """
        Analyseer een call transcript voor reserveringsresultaat.

        Zoekt naar:
        - Bevestiging van reservering
        - Bevestigingsnummer
        - Aangeboden alternatieven
        - Volgende stappen

        Returns:
            CallResult object met gestructureerde data
        """
        if not transcript:
            return CallResult(success=False, summary="Geen transcript beschikbaar")

        transcript_lower = transcript.lower()

        # Check voor bevestiging
        confirmation_patterns = [
            r"(is genoteerd|staat genoteerd)",
            r"(is gereserveerd|hebben gereserveerd)",
            r"(tot dan|we zien u|zie ik u)",
            r"(bevestig|bevestiging)",
            r"(is gelukt|prima|perfect|uitstekend)",
            r"(tafel voor u|tafeltje voor)",
        ]

        reservation_confirmed = any(
            re.search(pattern, transcript_lower)
            for pattern in confirmation_patterns
        )

        # Check voor afwijzing
        rejection_patterns = [
            r"(vol|volgeboekt|geen plek|geen plaats)",
            r"(niet mogelijk|helaas niet)",
            r"(gesloten|dicht)",
        ]

        was_rejected = any(
            re.search(pattern, transcript_lower)
            for pattern in rejection_patterns
        )

        # Zoek bevestigingsnummer
        confirmation_number = None
        conf_patterns = [
            r"nummer[:\s]+([A-Z0-9\-]+)",
            r"referentie[:\s]+([A-Z0-9\-]+)",
            r"bevestigingsnummer[:\s]+([A-Z0-9\-]+)",
            r"#\s*([A-Z0-9\-]+)",
        ]

        for pattern in conf_patterns:
            match = re.search(pattern, transcript, re.IGNORECASE)
            if match:
                confirmation_number = match.group(1)
                break

        # Zoek alternatieven
        alternative_offered = None
        alt_patterns = [
            r"(anders|andere tijd|andere dag|later|eerder)[^.]*(?:om|voor|rond)\s+(\d{1,2}[:.]\d{2}|\d{1,2}\s*uur)",
            r"(\d{1,2}[:.]\d{2}|\d{1,2}\s*uur)[^.]*(?:wel|nog|is er)",
        ]

        for pattern in alt_patterns:
            match = re.search(pattern, transcript_lower)
            if match:
                alternative_offered = match.group(0)
                break

        # Bepaal overall success
        success = reservation_confirmed and not was_rejected

        # Bouw summary
        if success:
            summary = "Reservering bevestigd"
            if confirmation_number:
                summary += f" (#{confirmation_number})"
        elif was_rejected:
            summary = "Restaurant was vol of kon niet reserveren"
            if alternative_offered:
                summary += f". Alternatief aangeboden: {alternative_offered}"
        else:
            summary = "Gesprek gevoerd, uitkomst onduidelijk"

        return CallResult(
            success=success,
            reservation_confirmed=reservation_confirmed,
            confirmation_number=confirmation_number,
            alternative_offered=alternative_offered,
            summary=summary,
            raw_transcript=transcript
        )
    
    async def update_assistant_to_connect_smart(
        self, assistant_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Update de Vapi assistant naar Connect Smart branding (naam, endCallMessage).
        Roep dit eenmalig aan na rebrand, of wanneer je de assistant in het dashboard wilt syncen.
        """
        aid = assistant_id or self.settings.vapi_assistant_id
        if not aid:
            return {
                "success": False,
                "error": "Geen VAPI_ASSISTANT_ID geconfigureerd"
            }
        payload = {
            "name": "Connect Smart - Sophie",
            "endCallMessage": "Oké, fijn. Dankjewel hè, doei!",
        }
        async with httpx.AsyncClient() as client:
            response = await client.patch(
                f"{self.VAPI_BASE_URL}/assistant/{aid}",
                headers=self.headers,
                json=payload,
                timeout=30.0
            )
            if response.status_code == 200:
                return {"success": True, "assistant": response.json()}
            return {
                "success": False,
                "error": response.text,
                "status_code": response.status_code
            }
    
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
