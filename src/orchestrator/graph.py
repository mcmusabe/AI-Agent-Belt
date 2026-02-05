"""
LangGraph Orchestrator - Coördineert alle agents.

Dit is het "brein" van het systeem dat bepaalt welke agent 
wanneer wordt ingezet en hoe de flow verloopt.
"""
from typing import TypedDict, Annotated, Literal, Optional, List, Dict, Any
from langgraph.graph import StateGraph, END
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, AIMessage, BaseMessage
import operator
import asyncio

from ..config import get_settings
from ..agents.browser import BrowserAgent
from ..agents.voice import VoiceAgent, normalize_e164
from ..agents.planner import PlannerAgent
from ..memory.supabase import get_memory_system
from ..tools.sms import send_sms
from ..tools.gmail import send_email
from ..tools.google_calendar import create_calendar_event


class AgentState(TypedDict):
    """State die door de graph wordt doorgegeven"""
    # Berichten geschiedenis
    messages: Annotated[List[BaseMessage], operator.add]
    
    # Huidige taak
    current_task: str
    
    # Plan voor uitvoering
    plan: Optional[Dict[str, Any]]
    
    # Resultaten van agents
    browser_result: Optional[Dict[str, Any]]
    voice_result: Optional[Dict[str, Any]]
    sms_result: Optional[Dict[str, Any]]
    email_result: Optional[Dict[str, Any]]
    calendar_result: Optional[Dict[str, Any]]
    
    # Metadata
    user_id: str
    needs_confirmation: bool
    final_response: Optional[str]
    
    # Volgende stap
    next_step: Optional[str]
    
    # User context uit geheugen
    user_context: Optional[Dict[str, Any]]

    # Confirm/clarify flow
    confirmed: bool
    clarification_questions: Optional[List[str]]
    pending_action: Optional[str]


def create_agent_graph():
    """
    Maak de LangGraph workflow voor agent orchestratie.
    
    Returns:
        Gecompileerde StateGraph
    """
    settings = get_settings()
    
    # Initialiseer LLM voor routing beslissingen
    llm = ChatAnthropic(
        model="claude-sonnet-4-20250514",
        api_key=settings.anthropic_api_key,
        max_tokens=1024,
    )
    
    # Initialiseer planner (stateless per request)
    planner = PlannerAgent()

    def _missing_info_questions(intent: Dict[str, Any]) -> List[str]:
        """Rule-based checks voor ontbrekende data bij acties."""
        intent_type = intent.get("intent", "")
        entities = intent.get("entities", {}) or {}
        questions: List[str] = []

        if intent_type == "bellen":
            if not entities.get("phone_number") and not entities.get("contact_name"):
                questions.append("Welk telefoonnummer moet ik bellen? (of geef een contactnaam)")

        if intent_type == "sms":
            if not entities.get("phone_number") and not entities.get("contact_name"):
                questions.append("Naar wie wil je een SMS sturen? (naam of telefoonnummer)")
            if not entities.get("message_body"):
                questions.append("Wat moet er in het SMS bericht staan?")

        if intent_type == "mail":
            if not entities.get("email_address") and not entities.get("contact_name"):
                questions.append("Naar wie wil je de mail sturen? (naam of e-mailadres)")
            if not entities.get("message_body"):
                questions.append("Wat moet er in de mail staan?")

        if intent_type == "agenda":
            if not entities.get("event_summary"):
                questions.append("Wat is de titel van de afspraak?")
            if not entities.get("date") and not entities.get("event_start"):
                questions.append("Wanneer is de afspraak? (datum en tijd)")

        if intent_type == "reservering":
            if not entities.get("venue"):
                questions.append("Bij welk restaurant/locatie wil je reserveren?")
            if not entities.get("date"):
                questions.append("Voor welke datum?")
            if not entities.get("time"):
                questions.append("Welke tijd?")
            if not entities.get("party_size"):
                questions.append("Voor hoeveel personen?")

        return questions
    
    # === NODE FUNCTIES ===
    
    async def analyze_request(state: AgentState) -> AgentState:
        """Analyseer het verzoek en maak een plan"""
        task = state["current_task"]
        user_id = state.get("user_id", "default")
        user_context = state.get("user_context")
        
        # Analyseer intent
        intent = await planner.analyze_intent(task)
        
        # Maak uitvoeringsplan
        plan = await planner.create_plan(task)
        
        # Bepaal of verduidelijking of bevestiging nodig is
        clarification_questions = list(intent.get("clarification_questions") or [])
        clarification_questions.extend(_missing_info_questions(intent))
        # Deduplicate while preserving order
        seen = set()
        clarification_questions = [
            q for q in clarification_questions
            if q and not (q in seen or seen.add(q))
        ]
        needs_clarification = len(clarification_questions) > 0

        confirmed = bool(state.get("confirmed"))
        needs_confirmation = False
        if not confirmed and not needs_clarification:
            intent_type = intent.get("intent", "")
            needs_confirmation = bool(
                plan.requires_user_confirmation
                or intent_type in ["bellen", "sms", "mail", "aankoop", "reservering"]
            )
        
        # Probeer voorkeuren te extraheren en op te slaan
        if settings.supabase_url and settings.supabase_anon_key:
            try:
                memory = get_memory_system()
                current_prefs = {}
                if user_context:
                    current_prefs = user_context.get("preferences", {})
                
                # Extract nieuwe voorkeuren
                new_prefs = await planner.extract_preferences(task, current_prefs)
                
                # Sla op als er nieuwe voorkeuren zijn
                if new_prefs:
                    await memory.update_user_preferences(user_id, new_prefs)
                    
                    # Sla ook op als memory voor context
                    for key, value in new_prefs.items():
                        await memory.add_memory(
                            telegram_id=user_id,
                            memory_type="preference",
                            content=f"{key}: {value}",
                            importance=6
                        )
            except Exception as e:
                pass  # Memory niet beschikbaar
        
        return {
            **state,
            "plan": {
                "goal": plan.goal,
                "steps": [s.model_dump() for s in plan.steps],
                "intent": intent
            },
            "needs_confirmation": needs_confirmation,
            "clarification_questions": clarification_questions,
            "pending_action": task if needs_confirmation else None,
            "messages": [AIMessage(content=f"Plan gemaakt: {plan.goal}")]
        }
    
    async def execute_browser_task(state: AgentState) -> AgentState:
        """Voer browser taak uit of gebruik AI voor informatie"""
        plan = state.get("plan", {})
        task = state["current_task"]
        intent = plan.get("intent", {})
        user_context = state.get("user_context")
        
        # Bouw context prompt als we user info hebben
        context_prompt = ""
        if user_context:
            user_name = user_context.get("user", {}).get("name", "")
            preferences = user_context.get("preferences", {})
            memories = user_context.get("memories", [])
            recent = user_context.get("recent_messages", [])
            
            if user_name:
                context_prompt += f"\nDe gebruiker heet {user_name}."
            if preferences:
                context_prompt += f"\nHun voorkeuren: {preferences}"
            if memories:
                memory_text = "; ".join([f"{m['type']}: {m['content']}" for m in memories[:5]])
                context_prompt += f"\nBelangrijke info over deze gebruiker: {memory_text}"
            if recent:
                recent_text = "\n".join([f"- {m['role']}: {m['content'][:100]}..." for m in recent[-3:]])
                context_prompt += f"\nRecente berichten:\n{recent_text}"
        
        # Voor informatieve vragen, gebruik direct Claude
        if intent.get("intent") in ["informatie", "vraag"]:
            # Beantwoord direct met Claude
            response = await llm.ainvoke([
                HumanMessage(content=f"""Je bent een behulpzame Nederlandse AI assistent van Connect Smart.
{context_prompt}

Beantwoord de volgende vraag zo goed mogelijk:

{task}

Geef een nuttig en informatief antwoord in het Nederlands. Wees vriendelijk en persoonlijk.""")
            ])
            
            return {
                **state,
                "browser_result": {"success": True, "result": response.content},
                "messages": [AIMessage(content=response.content)]
            }
        
        # Voor andere taken, probeer browser (als geconfigureerd)
        browser_steps = [s for s in plan.get("steps", []) if s.get("agent_type") == "browser"]
        
        if browser_steps:
            step_description = browser_steps[0]["description"]
        else:
            step_description = task
        
        browser_agent = BrowserAgent()
        try:
            result = await browser_agent.execute_task(step_description)
            
            return {
                **state,
                "browser_result": result,
                "messages": [AIMessage(content=f"Browser taak voltooid: {result.get('result', 'Geen resultaat')}")]
            }
        except Exception as e:
            # Fallback: beantwoord met Claude als browser niet werkt
            error_msg = str(e)
            
            # Als het een configuratie fout is, gebruik Claude
            if "BROWSER_USE_API_KEY" in error_msg or "API_KEY" in error_msg:
                response = await llm.ainvoke([
                    HumanMessage(content=f"""Je bent een behulpzame Nederlandse AI assistent van Connect Smart.
{context_prompt}

De gebruiker vroeg: {task}

Browser automatisering is niet beschikbaar. Beantwoord de vraag zo goed mogelijk met je eigen kennis.
Als het gaat om een reservering of actie die je niet kunt uitvoeren, leg dan uit wat de gebruiker zelf kan doen.
Wees vriendelijk en persoonlijk.""")
                ])
                
                return {
                    **state,
                    "browser_result": {"success": True, "result": response.content},
                    "messages": [AIMessage(content=response.content)]
                }
            
            return {
                **state,
                "browser_result": {"success": False, "error": error_msg},
                "messages": [AIMessage(content=f"Browser taak mislukt: {error_msg}")]
            }
        finally:
            try:
                await browser_agent.close()
            except:
                pass
    
    async def execute_voice_task(state: AgentState) -> AgentState:
        """Voer voice/telefoon taak uit - BEL ECHT via Vapi"""
        import re
        from datetime import datetime
        import pytz

        task = state["current_task"]
        plan = state.get("plan", {})
        intent = plan.get("intent", {})
        entities = intent.get("entities", {}) or {}
        user_id = state.get("user_id", "default")

        # Gebruik gedeelde contact resolutie
        contact_info = await _resolve_contact(user_id, task, intent)
        raw_number = contact_info.get("phone_number")

        # Fallback: probeer telefoonnummer uit de taak te extracten
        if not raw_number:
            phone_match = re.search(r'\+?\d[\d\s\-]{8,}', task)
            raw_number = phone_match.group() if phone_match else None

        # Voeg contactnaam toe aan task context
        if contact_info.get("contact"):
            task = f"{task} (Contact: {contact_info['contact']['name']})"

        if not raw_number:
            return {
                **state,
                "voice_result": {"success": False, "error": "Geen telefoonnummer gevonden in je verzoek"},
                "messages": [AIMessage(content="❌ Ik kon geen telefoonnummer vinden. Geef een nummer op zoals: +31612345678")]
            }
        
        # Strikte E.164-normalisatie (06 -> +31, alleen + en cijfers naar Vapi)
        phone_number = normalize_e164(raw_number)
        if not phone_number:
            return {
                **state,
                "voice_result": {"success": False, "error": "Ongeldig telefoonnummer."},
                "messages": [AIMessage(content="❌ Ongeldig telefoonnummer. Gebruik bijv. +31612345678 of 0612345678")]
            }
        
        # Bepaal tijdstip voor begroeting
        try:
            nl_tz = pytz.timezone('Europe/Amsterdam')
            now = datetime.now(nl_tz)
            hour = now.hour
        except:
            hour = 12  # Default middag
        
        if 6 <= hour < 12:
            greeting = "Goedemorgen"
        elif 12 <= hour < 18:
            greeting = "Goedemiddag"
        else:
            greeting = "Goedenavond"
        
        # Bepaal het doel van het gesprek uit de taak
        # Vapi/ElevenLabs best practices: Identity, Style, Response guidelines, emotie, natuurlijke spraak
        system_prompt = f"""
[IDENTITEIT]
Je bent Sophie en je belt namens Connect Smart. Je bent een echt persoon die belt – geen bot, geen script. Je hebt gevoelens en reageert op de ander.

[CONTEXT / WAT JE MOET REGELEN]
\"\"\"{task}\"\"\"

[STIJL – NATUURLIJK GESPREK]
- Praat zoals in een normaal telefoongesprek: informeel, warm, korte zinnen. "Hoi", "oké", "even kijken", "momentje".
- Geen formele of bot-achtige zinnen: geen "u spreekt met", "mag ik u kort iets vragen", "hoe kan ik u helpen", "is er nog iets anders", "graag gedaan" in een stijve vorm. Varieer je antwoorden.
- Antwoord inhoudelijk op wat de ander zegt; reageer niet met standaardzinnen. Soms een kort "Ja" of "Oké" of "Hmm" is genoeg.
- Luister goed en reageer snel. Niet te lang monologen; laat de ander ook aan het woord.

[EMOTIONEEL MEEBEWEGEN]
- Reageer op de toon van de ander: klinkt iemand geïrriteerd of haastig → wees kort en begripvol; klinkt iemand vriendelijk → praat mee; klinkt iemand onzeker → stel gerust.
- Toon af en toe begrip of meebewegen ("Snap ik", "Ach ja", "Klinkt vervelend") voordat je met oplossingen komt. Zo merk je niet als bot.

[VOICE-SPECIFIEK]
- Houd antwoorden kort (voice): 1–3 zinnen per beurt is vaak genoeg. Eén vraag of onderwerp per keer.
- Als de transcriptie iets raars geeft ("bekola", "blootje"), begrijp de bedoeling: cola, broodje, etc. Bevestig gewoon ("een cola, oké").
- Nederlandse woorden: biertje, broodje, cola, koffie, thee, lunch, bestelling, hete kip, etc.
- Als iets niet kan: denk mee over alternatieven. Rond af met een korte samenvatting van wat er is afgesproken.

[VOICEMAIL]
- Bandje/voicemail ("uw stem wordt niet waargenomen", "als u klaar bent beëindig", "toets 1") → één keer: "Geen probleem, ik bel later nog eens. Doei." en opbergen. Niet blijven "hallo" zeggen.

[Tijd] Nu {hour}:00 uur. Gebruik een passende begroeting ({greeting}).
"""

        # Eerste zin: kort en natuurlijk, taakgericht (geen vaste "mag ik u kort iets vragen")
        lower_task = task.lower()
        if "open" in lower_task:
            first_message = f"{greeting}, met Sophie van Connect Smart. Ik bel even om de openingstijden te checken, komt dat uit?"
        elif "reserv" in lower_task or "tafel" in lower_task:
            first_message = f"{greeting}, met Sophie van Connect Smart. Ik bel even voor een reservering, komt dat uit?"
        elif "ophalen" in lower_task or "ophaal" in lower_task or ("lunch" in lower_task and ("komt" in lower_task or "ophalen" in lower_task or "melden" in lower_task)):
            first_message = "Hoi, met Sophie van Connect Smart. Ik bel even om te zeggen dat de lunch eraan komt om op te halen."
        else:
            first_message = f"{greeting}, met Sophie van Connect Smart. Ik bel even met een korte vraag, komt dat uit?"
        
        voice_agent = VoiceAgent()
        try:
            # ECHTE CALL via Vapi
            call_result = await voice_agent.create_call(
                phone_number=phone_number,
                first_message=first_message,
                system_prompt=system_prompt
            )
            
            if call_result.get("success"):
                call_data = call_result.get("call", {})
                call_id = call_data.get("id", "onbekend")
                
                result = {
                    "success": True,
                    "message": f"📞 Gesprek gestart naar {phone_number}",
                    "call_id": call_id,
                    "status": "calling",
                    "phone_number": phone_number,
                    "preflight": call_result.get("preflight")
                }

                if settings.supabase_url and settings.supabase_anon_key:
                    try:
                        memory = get_memory_system()
                        intent_type = (intent.get("intent") or "")
                        call_type = "restaurant_reservation" if intent_type == "reservering" else "general"
                        await memory.log_call(
                            telegram_id=user_id,
                            call_id=call_id,
                            phone_number=phone_number,
                            call_type=call_type,
                            metadata={
                                "source": "orchestrator",
                                "task": task,
                                "preflight": call_result.get("preflight")
                            }
                        )
                    except Exception:
                        pass
                return {
                    **state,
                    "voice_result": result,
                    "messages": [AIMessage(content=f"📞 Ik bel nu {phone_number}... Het gesprek is gestart! (Call ID: {call_id})")]
                }
            else:
                error = call_result.get("error", "Onbekende fout")
                return {
                    **state,
                    "voice_result": {"success": False, "error": error},
                    "messages": [AIMessage(content=f"❌ Kon niet bellen: {error}")]
                }
                
        except Exception as e:
            return {
                **state,
                "voice_result": {"success": False, "error": str(e)},
                "messages": [AIMessage(content=f"❌ Fout bij bellen: {str(e)}")]
            }
    
    async def _resolve_contact(user_id: str, task: str, intent: Dict[str, Any]) -> Dict[str, Any]:
        """
        Probeer een contact te resolven uit de taak en intent entities.

        Returns dict met:
            phone_number, email, contact_name, contact (volledig record)
        """
        import re as _re

        entities = intent.get("entities", {}) or {}
        result = {
            "phone_number": entities.get("phone_number"),
            "email": entities.get("email_address"),
            "contact_name": entities.get("contact_name"),
            "contact": None,
        }

        if not settings.supabase_url or not settings.supabase_anon_key:
            return result

        try:
            memory = get_memory_system()

            # Als er al een expliciet nummer én email is, klaar
            if result["phone_number"] and result["email"]:
                return result

            # Zoek contact op naam
            contact_name = result["contact_name"]
            if not contact_name:
                # Probeer naam te extraheren uit de taak
                patterns = [
                    r'(?:bel|sms|mail|stuur)\s+(?:eens\s+)?(?:naar\s+)?(\w+)',
                ]
                for pattern in patterns:
                    match = _re.search(pattern, task.lower())
                    if match:
                        potential = match.group(1).strip()
                        # Filter keywords die geen naam zijn
                        if potential not in (
                            "een", "het", "de", "naar", "dat", "sms", "mail",
                            "email", "mij", "me", "hem", "haar",
                        ):
                            contact_name = potential
                            break

            if contact_name:
                contact = await memory.get_contact_by_name(user_id, contact_name)
                if contact:
                    result["contact"] = contact
                    result["contact_name"] = contact.get("name")
                    if not result["phone_number"]:
                        result["phone_number"] = contact.get("phone_number")
                    if not result["email"]:
                        result["email"] = contact.get("email")
        except Exception:
            pass

        return result

    async def execute_sms_task(state: AgentState) -> AgentState:
        """Verstuur een SMS bericht"""
        import re

        task = state["current_task"]
        plan = state.get("plan", {})
        intent = plan.get("intent", {})
        entities = intent.get("entities", {}) or {}
        user_id = state.get("user_id", "default")

        # Resolve contact
        contact_info = await _resolve_contact(user_id, task, intent)
        phone_number = contact_info.get("phone_number")

        # Probeer telefoonnummer uit de taak als fallback
        if not phone_number:
            phone_match = re.search(r'\+?\d[\d\s\-]{8,}', task)
            if phone_match:
                phone_number = phone_match.group()

        if not phone_number:
            return {
                **state,
                "sms_result": {"success": False, "error": "Geen telefoonnummer gevonden"},
                "messages": [AIMessage(content="Ik kon geen telefoonnummer vinden. Geef een nummer op of sla een contact op met /contact.")]
            }

        # Normaliseer nummer
        phone_number = normalize_e164(phone_number)
        if not phone_number:
            return {
                **state,
                "sms_result": {"success": False, "error": "Ongeldig telefoonnummer"},
                "messages": [AIMessage(content="Ongeldig telefoonnummer. Gebruik bijv. +31612345678 of 0612345678")]
            }

        # Bepaal bericht inhoud
        message_body = entities.get("message_body") or ""
        if not message_body:
            # Probeer bericht te extraheren: "sms jan dat ik later kom" → "ik later kom"
            dat_match = re.search(r'\bdat\b\s+(.+)', task, re.IGNORECASE)
            if dat_match:
                message_body = dat_match.group(1).strip()
            else:
                # Verwijder het commando-gedeelte en gebruik de rest
                cleaned = re.sub(
                    r'^(?:stuur\s+)?(?:sms|een\s+sms)\s+(?:naar\s+)?(?:\w+\s+)?',
                    '', task, flags=re.IGNORECASE
                ).strip()
                message_body = cleaned if cleaned else task

        contact_display = contact_info.get("contact_name") or phone_number

        try:
            result = await send_sms(to_number=phone_number, body=message_body)

            if result.get("success"):
                # Log in memory
                if settings.supabase_url and settings.supabase_anon_key:
                    try:
                        memory = get_memory_system()
                        conv = await memory.get_active_conversation(user_id)
                        if conv:
                            await memory.add_message(
                                conversation_id=conv["id"],
                                role="assistant",
                                content=f"[SMS verstuurd] Naar: {contact_display} ({phone_number})\nBericht: {message_body}",
                                metadata={"type": "sms", "to": phone_number, "message_sid": result.get("message_sid")}
                            )
                    except Exception:
                        pass

                return {
                    **state,
                    "sms_result": result,
                    "messages": [AIMessage(content=f"SMS verstuurd naar {contact_display} ({phone_number}):\n\"{message_body}\"")]
                }
            else:
                error = result.get("error", "Onbekende fout")
                return {
                    **state,
                    "sms_result": result,
                    "messages": [AIMessage(content=f"Kon SMS niet versturen: {error}")]
                }
        except (ValueError, Exception) as e:
            return {
                **state,
                "sms_result": {"success": False, "error": str(e)},
                "messages": [AIMessage(content=f"Fout bij versturen SMS: {str(e)}")]
            }

    async def execute_email_task(state: AgentState) -> AgentState:
        """Verstuur een e-mail via Gmail"""
        task = state["current_task"]
        plan = state.get("plan", {})
        intent = plan.get("intent", {})
        entities = intent.get("entities", {}) or {}
        user_id = state.get("user_id", "default")

        # Resolve contact
        contact_info = await _resolve_contact(user_id, task, intent)
        to_email = contact_info.get("email") or entities.get("email_address")

        if not to_email:
            return {
                **state,
                "email_result": {"success": False, "error": "Geen e-mailadres gevonden"},
                "messages": [AIMessage(content="Ik kon geen e-mailadres vinden. Geef een adres op of sla een contact op met /contact.")]
            }

        subject = entities.get("subject") or "Bericht via Connect Smart"
        body_text = entities.get("message_body") or task

        contact_display = contact_info.get("contact_name") or to_email

        try:
            result = await asyncio.to_thread(
                send_email,
                to_email=to_email,
                subject=subject,
                body_text=body_text,
            )

            # Log in memory
            if settings.supabase_url and settings.supabase_anon_key:
                try:
                    memory = get_memory_system()
                    conv = await memory.get_active_conversation(user_id)
                    if conv:
                        await memory.add_message(
                            conversation_id=conv["id"],
                            role="assistant",
                            content=f"[E-mail verstuurd] Naar: {contact_display} ({to_email})\nOnderwerp: {subject}",
                            metadata={"type": "email", "to": to_email}
                        )
                except Exception:
                    pass

            return {
                **state,
                "email_result": {"success": True, "to": to_email, "subject": subject},
                "messages": [AIMessage(content=f"E-mail verstuurd naar {contact_display} ({to_email})\nOnderwerp: {subject}")]
            }
        except (ValueError, Exception) as e:
            return {
                **state,
                "email_result": {"success": False, "error": str(e)},
                "messages": [AIMessage(content=f"Fout bij versturen e-mail: {str(e)}")]
            }

    async def execute_calendar_task(state: AgentState) -> AgentState:
        """Maak een agenda-afspraak in Google Calendar"""
        from datetime import datetime, timedelta
        import pytz

        task = state["current_task"]
        plan = state.get("plan", {})
        intent = plan.get("intent", {})
        entities = intent.get("entities", {}) or {}
        user_id = state.get("user_id", "default")

        summary = entities.get("event_summary") or task
        nl_tz = pytz.timezone("Europe/Amsterdam")
        now = datetime.now(nl_tz)

        # Probeer datum/tijd te parsen uit entities
        event_start_str = entities.get("event_start") or ""
        event_end_str = entities.get("event_end") or ""
        date_str = entities.get("date") or ""
        time_str = entities.get("time") or ""

        start_dt = None
        end_dt = None

        # Probeer ISO parse van event_start
        if event_start_str:
            try:
                start_dt = datetime.fromisoformat(event_start_str)
                if start_dt.tzinfo is None:
                    start_dt = nl_tz.localize(start_dt)
            except (ValueError, TypeError):
                pass

        # Fallback: combineer date + time
        if not start_dt and (date_str or time_str):
            import re
            # Probeer "morgen", "overmorgen", specifieke datum
            target_date = now.date()
            lower_date = date_str.lower() if date_str else ""
            if "morgen" in lower_date and "overmorgen" not in lower_date:
                target_date = (now + timedelta(days=1)).date()
            elif "overmorgen" in lower_date:
                target_date = (now + timedelta(days=2)).date()
            elif date_str:
                # Probeer dd-mm of dd-mm-yyyy
                date_match = re.search(r'(\d{1,2})[-/](\d{1,2})(?:[-/](\d{2,4}))?', date_str)
                if date_match:
                    day = int(date_match.group(1))
                    month = int(date_match.group(2))
                    year = int(date_match.group(3)) if date_match.group(3) else now.year
                    if year < 100:
                        year += 2000
                    try:
                        target_date = datetime(year, month, day).date()
                    except ValueError:
                        pass

            # Parse tijd
            target_hour, target_minute = 9, 0  # default
            if time_str:
                time_match = re.search(r'(\d{1,2})[:\.](\d{2})', time_str)
                if time_match:
                    target_hour = int(time_match.group(1))
                    target_minute = int(time_match.group(2))
                else:
                    hour_match = re.search(r'(\d{1,2})\s*(?:uur|u)', time_str)
                    if hour_match:
                        target_hour = int(hour_match.group(1))

            start_dt = nl_tz.localize(datetime(
                target_date.year, target_date.month, target_date.day,
                target_hour, target_minute
            ))

        if not start_dt:
            return {
                **state,
                "calendar_result": {"success": False, "error": "Kon geen datum/tijd bepalen"},
                "messages": [AIMessage(content="Ik kon geen datum of tijd bepalen voor de afspraak. Geef bijv. 'morgen om 10:00'.")]
            }

        # Eindtijd: gebruik event_end of default 1 uur
        if event_end_str:
            try:
                end_dt = datetime.fromisoformat(event_end_str)
                if end_dt.tzinfo is None:
                    end_dt = nl_tz.localize(end_dt)
            except (ValueError, TypeError):
                pass
        if not end_dt:
            end_dt = start_dt + timedelta(hours=1)

        try:
            event = await asyncio.to_thread(
                create_calendar_event,
                summary=summary,
                start_iso=start_dt.isoformat(),
                end_iso=end_dt.isoformat(),
                timezone="Europe/Amsterdam",
            )

            # Log in memory
            if settings.supabase_url and settings.supabase_anon_key:
                try:
                    memory = get_memory_system()
                    conv = await memory.get_active_conversation(user_id)
                    if conv:
                        await memory.add_message(
                            conversation_id=conv["id"],
                            role="assistant",
                            content=f"[Agenda] {summary} op {start_dt.strftime('%d-%m-%Y %H:%M')}",
                            metadata={"type": "calendar", "event_id": event.get("id") if isinstance(event, dict) else None}
                        )
                except Exception:
                    pass

            time_display = start_dt.strftime("%d-%m-%Y om %H:%M")
            return {
                **state,
                "calendar_result": {"success": True, "event": event, "summary": summary, "start": start_dt.isoformat()},
                "messages": [AIMessage(content=f"Afspraak aangemaakt in je agenda:\n\"{summary}\" op {time_display}")]
            }
        except (ValueError, Exception) as e:
            return {
                **state,
                "calendar_result": {"success": False, "error": str(e)},
                "messages": [AIMessage(content=f"Fout bij aanmaken afspraak: {str(e)}")]
            }

    async def generate_response(state: AgentState) -> AgentState:
        """Genereer de finale response voor de gebruiker"""
        browser_result = state.get("browser_result")
        voice_result = state.get("voice_result")
        plan = state.get("plan", {})

        clarification_questions = state.get("clarification_questions") or []
        if clarification_questions:
            lines = ["Ik heb nog een paar vragen om dit goed te regelen:"]
            for q in clarification_questions[:4]:
                lines.append(f"- {q}")
            final_response = "\n".join(lines)
            return {
                **state,
                "final_response": final_response,
                "messages": [AIMessage(content=final_response)]
            }

        if state.get("needs_confirmation"):
            goal = (plan.get("goal") or state.get("current_task") or "").strip()
            final_response = (
                f"Ik kan dit voor je regelen: {goal}\n"
                "Wil je dat ik nu doorga? (ja/nee)"
            )
            return {
                **state,
                "final_response": final_response,
                "messages": [AIMessage(content=final_response)]
            }
        
        # Als browser_result een AI response bevat, gebruik die direct
        if browser_result and browser_result.get("success"):
            result_text = browser_result.get("result", "")
            if result_text and len(result_text) > 50:  # Waarschijnlijk een AI response
                return {
                    **state,
                    "final_response": result_text,
                    "messages": [AIMessage(content=result_text)]
                }
        
        # Combineer resultaten
        results_summary = []
        
        if browser_result:
            if browser_result.get("success"):
                results_summary.append(f"✅ {browser_result.get('result', 'Taak uitgevoerd')}")
            else:
                results_summary.append(f"❌ Online taak mislukt: {browser_result.get('error', 'Onbekende fout')}")
        
        if voice_result:
            if voice_result.get("success"):
                results_summary.append(f"📞 {voice_result.get('message', 'Gesprek voltooid')}")
            else:
                results_summary.append(f"❌ Telefoongesprek mislukt: {voice_result.get('error', 'Onbekende fout')}")

        sms_result = state.get("sms_result")
        if sms_result:
            if sms_result.get("success"):
                results_summary.append(f"📱 SMS verstuurd naar {sms_result.get('to', 'onbekend')}")
            else:
                results_summary.append(f"❌ SMS mislukt: {sms_result.get('error', 'Onbekende fout')}")

        email_result = state.get("email_result")
        if email_result:
            if email_result.get("success"):
                results_summary.append(f"📧 E-mail verstuurd naar {email_result.get('to', 'onbekend')}")
            else:
                results_summary.append(f"❌ E-mail mislukt: {email_result.get('error', 'Onbekende fout')}")

        calendar_result = state.get("calendar_result")
        if calendar_result:
            if calendar_result.get("success"):
                results_summary.append(f"📅 Afspraak aangemaakt: {calendar_result.get('summary', '')}")
            else:
                results_summary.append(f"❌ Agenda fout: {calendar_result.get('error', 'Onbekende fout')}")

        final_response = "\n".join(results_summary) if results_summary else "Taak verwerkt."
        
        return {
            **state,
            "final_response": final_response,
            "messages": [AIMessage(content=final_response)]
        }
    
    def route_after_analysis(state: AgentState) -> str:
        """Bepaal de volgende stap na analyse"""
        plan = state.get("plan", {})
        intent = plan.get("intent", {})
        steps = plan.get("steps", [])
        task = state.get("current_task", "").lower()

        if state.get("clarification_questions"):
            return "generate_response"

        if state.get("needs_confirmation"):
            return "generate_response"
        
        # Check direct op keywords in de taak
        if any(word in task for word in ["bel ", "bel+", "bellen", "telefoneer", "call "]):
            return "execute_voice"

        if any(word in task for word in ["sms ", "stuur sms", "stuur een sms", "berichtje"]):
            return "execute_sms"

        if any(word in task for word in ["mail ", "email ", "stuur mail", "stuur email", "stuur een mail"]):
            return "execute_email"

        if any(word in task for word in ["agenda", "afspraak", "meeting", "plan in", "inplannen"]):
            return "execute_calendar"

        # Check intent van planner
        intent_type = intent.get("intent", "")
        if intent_type == "bellen":
            return "execute_voice"
        if intent_type == "sms":
            return "execute_sms"
        if intent_type == "mail":
            return "execute_email"
        if intent_type == "agenda":
            return "execute_calendar"
        if intent_type == "herinnering":
            return "generate_response"  # Uitleg over /herinner command

        if not steps:
            return "generate_response"

        # Bepaal welke agent eerst moet op basis van plan
        first_step = steps[0]
        agent_type = first_step.get("agent_type", "browser")

        agent_to_node = {
            "voice": "execute_voice",
            "sms": "execute_sms",
            "email": "execute_email",
            "calendar": "execute_calendar",
            "browser": "execute_browser",
        }
        return agent_to_node.get(agent_type, "execute_browser")
    
    def route_after_browser(state: AgentState) -> str:
        """Bepaal volgende stap na browser taak"""
        plan = state.get("plan", {})
        steps = plan.get("steps", [])
        browser_result = state.get("browser_result", {})
        
        # Check of we voice moeten doen (bijv. als online niet lukte)
        if not browser_result.get("success"):
            # Zoek fallback
            voice_steps = [s for s in steps if s.get("agent_type") == "voice"]
            if voice_steps:
                return "execute_voice"
        
        return "generate_response"
    
    def route_after_voice(state: AgentState) -> str:
        """Bepaal volgende stap na voice taak"""
        return "generate_response"
    
    # === BOUW DE GRAPH ===
    
    workflow = StateGraph(AgentState)
    
    # Voeg nodes toe
    workflow.add_node("analyze", analyze_request)
    workflow.add_node("execute_browser", execute_browser_task)
    workflow.add_node("execute_voice", execute_voice_task)
    workflow.add_node("execute_sms", execute_sms_task)
    workflow.add_node("execute_email", execute_email_task)
    workflow.add_node("execute_calendar", execute_calendar_task)
    workflow.add_node("generate_response", generate_response)

    # Voeg edges toe
    workflow.set_entry_point("analyze")

    workflow.add_conditional_edges(
        "analyze",
        route_after_analysis,
        {
            "execute_browser": "execute_browser",
            "execute_voice": "execute_voice",
            "execute_sms": "execute_sms",
            "execute_email": "execute_email",
            "execute_calendar": "execute_calendar",
            "generate_response": "generate_response",
        }
    )

    workflow.add_conditional_edges(
        "execute_browser",
        route_after_browser,
        {
            "execute_voice": "execute_voice",
            "generate_response": "generate_response"
        }
    )

    workflow.add_conditional_edges(
        "execute_voice",
        route_after_voice,
        {
            "generate_response": "generate_response"
        }
    )

    # Nieuwe nodes gaan direct naar generate_response
    workflow.add_edge("execute_sms", "generate_response")
    workflow.add_edge("execute_email", "generate_response")
    workflow.add_edge("execute_calendar", "generate_response")

    workflow.add_edge("generate_response", END)
    
    # Compileer de graph
    return workflow.compile()


# Singleton instance
_graph = None

def get_agent_graph():
    """Get or create the agent graph"""
    global _graph
    if _graph is None:
        _graph = create_agent_graph()
    return _graph


async def process_request(
    user_message: str,
    user_id: str = "default",
    confirmed: bool = False
) -> Dict[str, Any]:
    """
    Verwerk een gebruikersverzoek door de volledige agent pipeline.
    
    Args:
        user_message: Het verzoek van de gebruiker
        user_id: ID van de gebruiker
        
    Returns:
        Dict met response, call_id (indien voice call), en andere metadata
    """
    graph = get_agent_graph()
    settings = get_settings()
    
    # Haal user context uit memory als beschikbaar
    user_context = None
    if settings.supabase_url and settings.supabase_anon_key:
        try:
            memory = get_memory_system()
            user_context = await memory.get_user_context(user_id)
        except Exception as e:
            pass  # Memory niet beschikbaar, ga door zonder context
    
    initial_state: AgentState = {
        "messages": [HumanMessage(content=user_message)],
        "current_task": user_message,
        "plan": None,
        "browser_result": None,
        "voice_result": None,
        "sms_result": None,
        "email_result": None,
        "calendar_result": None,
        "user_id": user_id,
        "needs_confirmation": False,
        "final_response": None,
        "next_step": None,
        "user_context": user_context,
        "confirmed": confirmed,
        "clarification_questions": None,
        "pending_action": None,
    }
    
    # Run de graph
    result = await graph.ainvoke(initial_state)
    
    # Extract call_id als er een voice call was
    voice_result = result.get("voice_result", {})
    call_id = voice_result.get("call_id") if voice_result else None
    
    return {
        "response": result.get("final_response", "Er is iets misgegaan bij het verwerken van je verzoek."),
        "call_id": call_id,
        "voice_result": voice_result,
        "sms_result": result.get("sms_result"),
        "email_result": result.get("email_result"),
        "calendar_result": result.get("calendar_result"),
        "needs_confirmation": result.get("needs_confirmation", False),
        "pending_action": result.get("pending_action"),
        "clarification_questions": result.get("clarification_questions"),
    }
