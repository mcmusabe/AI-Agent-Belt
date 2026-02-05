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
from ..agents.voice import VoiceAgent
from ..agents.planner import PlannerAgent
from ..memory.supabase import get_memory_system


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
    
    # Metadata
    user_id: str
    needs_confirmation: bool
    final_response: Optional[str]
    
    # Volgende stap
    next_step: Optional[str]
    
    # User context uit geheugen
    user_context: Optional[Dict[str, Any]]


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
    
    # Initialiseer agents
    planner = PlannerAgent()
    browser_agent = BrowserAgent()
    voice_agent = VoiceAgent()
    
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
        
        # Bepaal of bevestiging nodig is
        needs_confirmation = plan.requires_user_confirmation
        
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
        entities = intent.get("entities", {})
        user_id = state.get("user_id", "default")
        
        # Extract telefoonnummer uit de taak
        phone_match = re.search(r'\+?\d[\d\s\-]{8,}', task)
        phone_number = phone_match.group().replace(" ", "").replace("-", "") if phone_match else None
        
        # Als geen nummer gevonden, probeer contact lookup
        if not phone_number and settings.supabase_url and settings.supabase_anon_key:
            try:
                memory = get_memory_system()
                # Zoek naar een naam in de taak
                # Patronen zoals "bel Jan", "bel Restaurant De Kas", "bel mijn moeder"
                name_patterns = [
                    r'bel\s+(?:eens\s+)?(?:naar\s+)?(.+?)(?:\s+(?:om|voor|en|dat|of)|$)',
                    r'call\s+(.+?)(?:\s+(?:to|for|and|that|or)|$)',
                ]
                
                for pattern in name_patterns:
                    name_match = re.search(pattern, task.lower())
                    if name_match:
                        potential_name = name_match.group(1).strip()
                        # Filter out telefoonnummer-achtige strings
                        if not re.search(r'\d{5,}', potential_name):
                            contact = await memory.get_contact_by_name(user_id, potential_name)
                            if contact and contact.get("phone_number"):
                                phone_number = contact["phone_number"]
                                # Voeg contactnaam toe aan task context
                                task = f"{task} (Contact: {contact['name']})"
                                break
            except Exception as e:
                pass  # Memory niet beschikbaar
        
        if not phone_number:
            return {
                **state,
                "voice_result": {"success": False, "error": "Geen telefoonnummer gevonden in je verzoek"},
                "messages": [AIMessage(content="❌ Ik kon geen telefoonnummer vinden. Geef een nummer op zoals: +31612345678")]
            }
        
        # Zorg dat nummer internationaal formaat heeft
        if not phone_number.startswith("+"):
            if phone_number.startswith("0"):
                phone_number = "+31" + phone_number[1:]
            else:
                phone_number = "+" + phone_number
        
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
        # Let op: we willen NIET dat Sophie praat over "opdrachten" of "AI".
        # Ze moet klinken als een echte Nederlander die gewoon even belt.
        system_prompt = f"""
Je bent Sophie en je belt namens Connect Smart. Je belt namens een klant,
maar je praat zoals een normale Nederlandse collega die iemand even opbelt.

CONTEXT:
- Dit is in het kort wat de klant wil dat je regelt of bespreekt:
\"\"\"{task}\"\"\"

GEDRAG:
- Doe alsof jij zelf belt. Zeg NIET dat je een AI, digitale assistent of bot bent.
- Zeg NIET dat je een opdracht of taak uitvoert, noem ook geen "script".
- Spreek natuurlijk Nederlands:
  - Gebruik woorden als "hoi", "goedemorgen", "even kijken", "momentje", "oké".
  - Korte, duidelijke zinnen. Niet te formeel.
- Luister goed naar het antwoord en reageer snel. Niet te lang wachten of herhalen tenzij nodig.
- Als de transcriptie iets raars geeft (bijv. "bekola", "blootje", "de cola"), begrijp dan wat bedoeld wordt: "bekola"/"de cola" = cola, "blootje"/"bllotje" = broodje, etc. Bevestig gewoon de bedoeling ("een cola, oké").
- Ken Nederlandse woorden: biertje = bier, broodje = sandwich, cola, koffie, thee, lunch, bestelling, hete kip, etc.
- Als iets niet kan, denk mee over alternatieven.
- Vat aan het einde kort samen wat er is afgesproken.
- Wees beleefd maar niet overdreven formeel.

ANTWOORDAPPARAAT/VOICEMAIL:
- Als je een bandje of voicemail hoort (bijv. "uw stem wordt niet waargenomen", "als u klaar bent beëindig dit gesprek", "na de toon", "toets 1", "voor meer opties"), zeg dan één keer kort: "Geen probleem, ik bel later nog eens. Doei." en beëindig het gesprek. Ga niet meerdere keren "hallo" zeggen tegen een bandje.

HUIDIGE TIJD: Het is nu {hour}:00 uur (gebruik een passende begroeting zoals: {greeting}).
"""

        # Pas first_message aan op basis van de taak en tijdstip
        lower_task = task.lower()
        if "open" in lower_task:
            first_message = (
                f"{greeting}, u spreekt met Sophie van Connect Smart. "
                f"Ik bel even om te vragen wat uw openingstijden zijn."
            )
        elif "reserv" in lower_task or "tafel" in lower_task:
            first_message = (
                f"{greeting}, met Sophie van Connect Smart. "
                f"Ik wilde graag een reservering maken, is daar nog ruimte voor?"
            )
        else:
            first_message = (
                f"{greeting}, u spreekt met Sophie van Connect Smart. "
                f"Ik bel even namens iemand die u kent, mag ik u kort iets vragen?"
            )
        
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
                    "status": "calling"
                }
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
    
    async def generate_response(state: AgentState) -> AgentState:
        """Genereer de finale response voor de gebruiker"""
        browser_result = state.get("browser_result")
        voice_result = state.get("voice_result")
        plan = state.get("plan", {})
        
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
        
        # Check direct op "bel" in de taak - dan ALTIJD voice
        if any(word in task for word in ["bel ", "bel+", "bellen", "telefoneer", "call "]):
            return "execute_voice"
        
        # Check intent van planner
        intent_type = intent.get("intent", "")
        if intent_type == "bellen":
            return "execute_voice"
        
        if not steps:
            return "generate_response"
        
        # Bepaal welke agent eerst moet
        first_step = steps[0]
        agent_type = first_step.get("agent_type", "browser")
        
        if agent_type == "voice":
            return "execute_voice"
        elif agent_type == "browser":
            return "execute_browser"
        else:
            return "execute_browser"  # Default naar browser
    
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
    workflow.add_node("generate_response", generate_response)
    
    # Voeg edges toe
    workflow.set_entry_point("analyze")
    
    workflow.add_conditional_edges(
        "analyze",
        route_after_analysis,
        {
            "execute_browser": "execute_browser",
            "execute_voice": "execute_voice",
            "generate_response": "generate_response"
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


async def process_request(user_message: str, user_id: str = "default") -> Dict[str, Any]:
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
        "user_id": user_id,
        "needs_confirmation": False,
        "final_response": None,
        "next_step": None,
        "user_context": user_context
    }
    
    # Run de graph
    result = await graph.ainvoke(initial_state)
    
    # Extract call_id als er een voice call was
    voice_result = result.get("voice_result", {})
    call_id = voice_result.get("call_id") if voice_result else None
    
    return {
        "response": result.get("final_response", "Er is iets misgegaan bij het verwerken van je verzoek."),
        "call_id": call_id,
        "voice_result": voice_result
    }
