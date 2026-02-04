"""
Browser Agent - Gebruikt Browser-Use voor autonome website bediening.

Kan:
- Websites bezoeken en navigeren
- Formulieren invullen
- Reserveringen maken
- Tickets kopen
- Informatie zoeken
"""
import asyncio
import os
from typing import Optional
from browser_use import Agent, Browser

from ..config import get_settings


class BrowserAgent:
    """Agent die websites autonoom kan bedienen via Browser-Use"""
    
    def __init__(self, headless: bool = True):
        """
        Initialize browser agent.
        
        Args:
            headless: Run browser in headless mode (True voor productie)
        """
        self.settings = get_settings()
        # Set API key in environment for browser-use
        os.environ["ANTHROPIC_API_KEY"] = self.settings.anthropic_api_key
        self.browser: Optional[Browser] = None
        self.headless = headless
    
    async def initialize(self):
        """Initialiseer de browser"""
        if self.browser is None:
            # Productie mode: headless=True
            # Development mode: headless=False om te zien wat er gebeurt
            self.browser = Browser(headless=self.headless)
    
    async def execute_task(self, task: str, max_steps: int = 25) -> dict:
        """
        Voer een taak uit in de browser.
        
        Args:
            task: De taak om uit te voeren (bijv. "Reserveer een tafel bij Restaurant X")
            max_steps: Maximum aantal stappen
            
        Returns:
            dict met resultaat en eventuele details
        """
        await self.initialize()
        
        # Browser-use gebruikt zijn eigen LLM configuratie
        agent = Agent(
            task=task,
            browser=self.browser,
        )
        
        try:
            result = await agent.run(max_steps=max_steps)
            
            # Extract de finale output
            final_result = result.final_result() if result else None
            history = result.history() if result and hasattr(result, 'history') else []
            
            return {
                "success": True,
                "result": final_result,
                "steps_taken": len(history) if history else 0,
                "task": task
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "task": task
            }
    
    async def search_and_extract(self, query: str, site: Optional[str] = None) -> dict:
        """
        Zoek informatie op en extraheer relevante data.
        
        Args:
            query: Zoekopdracht
            site: Optioneel specifieke website om te doorzoeken
            
        Returns:
            dict met gevonden informatie
        """
        if site:
            task = f"Ga naar {site} en zoek naar: {query}. Geef een samenvatting van wat je vindt."
        else:
            task = f"Zoek op Google naar: {query}. Bezoek de meest relevante resultaten en geef een samenvatting."
        
        return await self.execute_task(task)
    
    async def make_reservation(
        self, 
        venue_name: str,
        date: str,
        time: str,
        party_size: int,
        name: str,
        email: Optional[str] = None,
        phone: Optional[str] = None,
        special_requests: Optional[str] = None
    ) -> dict:
        """
        Maak een reservering bij een restaurant of locatie.
        
        Args:
            venue_name: Naam van het restaurant/locatie
            date: Datum (bijv. "15 februari 2026")
            time: Tijd (bijv. "19:00")
            party_size: Aantal personen
            name: Naam voor de reservering
            email: E-mailadres (optioneel)
            phone: Telefoonnummer (optioneel)
            special_requests: Speciale verzoeken (optioneel)
            
        Returns:
            dict met reserveringsdetails
        """
        task = f"""
        Maak een reservering bij {venue_name}:
        - Datum: {date}
        - Tijd: {time}
        - Aantal personen: {party_size}
        - Naam: {name}
        {"- E-mail: " + email if email else ""}
        {"- Telefoon: " + phone if phone else ""}
        {"- Speciale verzoeken: " + special_requests if special_requests else ""}
        
        Stappen:
        1. Zoek de website van {venue_name}
        2. Vind de reserveringspagina
        3. Vul het formulier in met bovenstaande gegevens
        4. Bevestig de reservering
        5. Noteer het bevestigingsnummer als dat wordt gegeven
        
        Als online reserveren niet mogelijk is, geef aan dat telefonisch contact nodig is.
        """
        
        return await self.execute_task(task, max_steps=30)
    
    async def book_tickets(
        self,
        event_name: str,
        venue: str,
        date: str,
        num_tickets: int,
        name: str,
        email: str
    ) -> dict:
        """
        Koop tickets voor een evenement.
        
        Args:
            event_name: Naam van het evenement
            venue: Locatie
            date: Datum
            num_tickets: Aantal tickets
            name: Naam koper
            email: E-mail voor bevestiging
            
        Returns:
            dict met ticketdetails
        """
        task = f"""
        Koop {num_tickets} ticket(s) voor:
        - Evenement: {event_name}
        - Locatie: {venue}
        - Datum: {date}
        - Naam: {name}
        - E-mail: {email}
        
        Let op: NIET daadwerkelijk afrekenen zonder expliciete toestemming.
        Stop bij de betalingspagina en rapporteer de totale kosten.
        """
        
        return await self.execute_task(task, max_steps=30)
    
    async def close(self):
        """Sluit de browser"""
        if self.browser:
            try:
                await self.browser.stop()
            except Exception:
                pass  # Browser was al gesloten
            self.browser = None


# Convenience functie voor snelle taken
async def quick_browser_task(task: str) -> dict:
    """Voer snel een browser taak uit"""
    agent = BrowserAgent()
    try:
        return await agent.execute_task(task)
    finally:
        await agent.close()
