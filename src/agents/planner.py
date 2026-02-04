"""
Planner Agent - Analyseert opdrachten en bepaalt de beste aanpak.

Kan:
- Opdrachten analyseren en opsplitsen in stappen
- Bepalen welke tools/agents nodig zijn
- Prioriteiten stellen
- Alternatieven bedenken bij problemen
"""
from typing import List, Dict, Any, Optional
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field
import json

from ..config import get_settings


class TaskStep(BaseModel):
    """Een stap in het uitvoeringsplan"""
    step_number: int
    description: str
    agent_type: str = Field(description="browser, voice, of research")
    estimated_duration: str
    fallback: Optional[str] = None


class ExecutionPlan(BaseModel):
    """Volledig uitvoeringsplan"""
    goal: str
    steps: List[TaskStep]
    estimated_total_duration: str
    requires_user_confirmation: bool = False
    warnings: List[str] = []


class PlannerAgent:
    """Agent die taken plant en coördineert"""
    
    def __init__(self):
        self.settings = get_settings()
        self.llm = ChatAnthropic(
            model="claude-sonnet-4-20250514",
            api_key=self.settings.anthropic_api_key,
            max_tokens=2048,
        )
    
    async def create_plan(self, user_request: str, context: Optional[Dict] = None) -> ExecutionPlan:
        """
        Maak een uitvoeringsplan voor een gebruikersverzoek.
        
        Args:
            user_request: Het verzoek van de gebruiker
            context: Optionele context (bijv. eerdere gesprekken, voorkeuren)
            
        Returns:
            ExecutionPlan met gedetailleerde stappen
        """
        system_prompt = """Je bent een planning AI die gebruikersverzoeken analyseert en omzet in concrete uitvoeringsplannen.

Je hebt toegang tot de volgende agents:
1. BROWSER AGENT: Kan websites bezoeken, formulieren invullen, reserveringen maken online
2. VOICE AGENT: Kan telefonisch bellen naar bedrijven (restaurants, hotels, etc.)
3. RESEARCH AGENT: Kan informatie opzoeken en samenvatten

Regels:
- Splits complexe taken op in logische stappen
- Kies de meest efficiënte aanpak (online > telefoon indien mogelijk)
- Geef altijd een fallback optie als de primaire aanpak faalt
- Wees realistisch over tijdsinschattingen
- Markeer taken die gebruikersbevestiging nodig hebben (bijv. betalingen)

Geef je antwoord als JSON in dit formaat:
{
    "goal": "Hoofddoel van de taak",
    "steps": [
        {
            "step_number": 1,
            "description": "Wat er moet gebeuren",
            "agent_type": "browser|voice|research",
            "estimated_duration": "X minuten",
            "fallback": "Alternatief als dit faalt"
        }
    ],
    "estimated_total_duration": "X minuten",
    "requires_user_confirmation": true/false,
    "warnings": ["Eventuele waarschuwingen"]
}"""

        context_str = ""
        if context:
            context_str = f"\n\nContext: {json.dumps(context, ensure_ascii=False)}"
        
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=f"Gebruikersverzoek: {user_request}{context_str}")
        ]
        
        response = await self.llm.ainvoke(messages)
        
        # Parse de JSON response
        try:
            # Probeer JSON te extraheren uit de response
            content = response.content
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                content = content.split("```")[1].split("```")[0]
            
            plan_data = json.loads(content)
            return ExecutionPlan(**plan_data)
        except (json.JSONDecodeError, KeyError) as e:
            # Fallback plan als parsing faalt
            return ExecutionPlan(
                goal=user_request,
                steps=[
                    TaskStep(
                        step_number=1,
                        description=f"Uitvoeren: {user_request}",
                        agent_type="browser",
                        estimated_duration="5-10 minuten"
                    )
                ],
                estimated_total_duration="5-10 minuten",
                warnings=[f"Kon geen gedetailleerd plan maken: {str(e)}"]
            )
    
    async def analyze_intent(self, message: str) -> Dict[str, Any]:
        """
        Analyseer de intentie van een bericht.
        
        Args:
            message: Het bericht van de gebruiker
            
        Returns:
            dict met intent analyse
        """
        system_prompt = """Analyseer het bericht en bepaal:
1. De primaire intentie
2. Entiteiten (telefoonnummer, restaurant naam, datum, tijd, aantal personen, etc.)
3. Urgentie (hoog, normaal, laag)
4. Of er aanvullende informatie nodig is

BELANGRIJK: Als de gebruiker vraagt om te BELLEN of een telefoongesprek te voeren, is de intent ALTIJD "bellen".
Kijk naar woorden zoals: bel, bellen, telefoneer, call, telefoon, etc.

Geef je antwoord als JSON:
{
    "intent": "bellen|reservering|informatie|aankoop|vraag|anders",
    "entities": {
        "phone_number": "telefoonnummer indien genoemd",
        "venue": "naam indien genoemd",
        "date": "datum indien genoemd",
        "time": "tijd indien genoemd",
        "party_size": "aantal indien genoemd",
        "other": {}
    },
    "urgency": "hoog|normaal|laag",
    "needs_clarification": true/false,
    "clarification_questions": ["vraag1", "vraag2"]
}"""
        
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=message)
        ]
        
        response = await self.llm.ainvoke(messages)
        
        try:
            content = response.content
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                content = content.split("```")[1].split("```")[0]
            
            return json.loads(content)
        except json.JSONDecodeError:
            return {
                "intent": "anders",
                "entities": {},
                "urgency": "normaal",
                "needs_clarification": True,
                "clarification_questions": ["Kun je je vraag verduidelijken?"]
            }
    
    async def suggest_next_action(
        self, 
        current_state: Dict[str, Any],
        history: List[Dict[str, Any]]
    ) -> str:
        """
        Suggereer de volgende actie op basis van huidige staat en geschiedenis.
        
        Args:
            current_state: Huidige staat van de taak
            history: Geschiedenis van acties
            
        Returns:
            Suggestie voor volgende actie
        """
        system_prompt = """Op basis van de huidige staat en geschiedenis, wat is de beste volgende stap?
        Geef een concrete, uitvoerbare suggestie."""
        
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=f"Huidige staat: {json.dumps(current_state, ensure_ascii=False)}\n\nGeschiedenis: {json.dumps(history, ensure_ascii=False)}")
        ]
        
        response = await self.llm.ainvoke(messages)
        return response.content
