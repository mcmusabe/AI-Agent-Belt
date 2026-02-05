"""
Demo script om Connect Smart te testen.

Voert verschillende taken uit om te laten zien wat het systeem kan.
"""
import asyncio
from src.config import get_settings
from src.agents.browser import BrowserAgent
from src.agents.voice import VoiceAgent
from src.agents.planner import PlannerAgent
from src.orchestrator.graph import process_request


async def demo_planner():
    """Demo: Plan een restaurantreservering"""
    print("\n" + "="*60)
    print("🧠 DEMO: Planner Agent")
    print("="*60)
    
    planner = PlannerAgent()
    
    # Test intent analyse
    message = "Ik wil morgenavond met 4 personen eten bij De Librije om 19:30"
    print(f"\n📝 Bericht: {message}")
    
    intent = await planner.analyze_intent(message)
    print(f"\n🎯 Intent analyse:")
    print(f"   - Intent: {intent.get('intent')}")
    print(f"   - Entiteiten: {intent.get('entities')}")
    print(f"   - Urgentie: {intent.get('urgency')}")
    
    # Maak een plan
    plan = await planner.create_plan(message)
    print(f"\n📋 Uitvoeringsplan:")
    print(f"   - Doel: {plan.goal}")
    print(f"   - Geschatte tijd: {plan.estimated_total_duration}")
    for step in plan.steps:
        print(f"   - Stap {step.step_number}: {step.description} ({step.agent_type})")


async def demo_browser_search():
    """Demo: Zoek informatie met de browser"""
    print("\n" + "="*60)
    print("🌐 DEMO: Browser Agent - Zoeken")
    print("="*60)
    
    browser = BrowserAgent()
    
    try:
        task = "Ga naar google.nl en zoek naar 'beste restaurants Amsterdam 2026'. Geef de top 3 resultaten."
        print(f"\n📝 Taak: {task}")
        print("\n⏳ Browser wordt gestart... (dit kan even duren)")
        
        result = await browser.execute_task(task, max_steps=10)
        
        print(f"\n✅ Resultaat:")
        print(f"   - Succes: {result.get('success')}")
        print(f"   - Stappen: {result.get('steps_taken')}")
        if result.get('result'):
            print(f"   - Output: {result.get('result')[:500]}...")
            
    finally:
        await browser.close()


async def demo_voice_info():
    """Demo: Bekijk Vapi configuratie"""
    print("\n" + "="*60)
    print("📞 DEMO: Voice Agent - Configuratie")
    print("="*60)
    
    voice = VoiceAgent()
    
    # Check telefoon nummers
    print("\n🔍 Beschikbare telefoonnummers:")
    numbers = await voice.list_phone_numbers()
    
    if numbers.get("success"):
        phone_numbers = numbers.get("phone_numbers", [])
        if phone_numbers:
            for num in phone_numbers:
                print(f"   - {num.get('number')} ({num.get('id')})")
        else:
            print("   ⚠️ Geen telefoonnummers geconfigureerd")
            print("   💡 Koop een nummer op https://dashboard.vapi.ai/phone-numbers")
    else:
        print(f"   ❌ Error: {numbers.get('error')}")


async def demo_full_flow():
    """Demo: Volledige flow via orchestrator"""
    print("\n" + "="*60)
    print("🤖 DEMO: Volledige Agent Flow")
    print("="*60)
    
    message = "Zoek een goed Italiaans restaurant in Amsterdam voor vanavond"
    print(f"\n📱 Simulatie WhatsApp bericht: '{message}'")
    print("\n⏳ Verwerken via orchestrator...")
    
    result = await process_request(message, user_id="demo_user")
    
    print(f"\n📤 Response:")
    print(f"{result}")


async def demo_restaurant_call_preview():
    """Demo: Preview van een restaurant gesprek"""
    print("\n" + "="*60)
    print("📞 DEMO: Restaurant Bel Preview")
    print("="*60)
    
    print("""
    Als je een Vapi telefoonnummer hebt, kan de AI dit gesprek voeren:
    
    🤖 AI: "Goedemiddag, ik bel namens meneer Jansen. Ik zou graag 
           een tafel willen reserveren voor 4 personen op vrijdag 
           om 19:30. Is dat mogelijk?"
    
    👨‍🍳 Restaurant: "Helaas, we zitten die avond vol."
    
    🤖 AI: "Begrijpelijk. Zou het mogelijk zijn om aan de bar te 
           zitten? Of is er later op de avond nog plek?"
    
    👨‍🍳 Restaurant: "Laat me even kijken... We hebben om 21:00 
                     nog een tafel vrij."
    
    🤖 AI: "Uitstekend! Kunnen we die reserveren op naam van Jansen 
           voor 4 personen om 21:00?"
    
    👨‍🍳 Restaurant: "Ja, dat is genoteerd."
    
    🤖 AI: "Heel fijn, hartelijk dank! Tot vrijdag!"
    """)


async def main():
    """Run alle demos"""
    print("🚀 Connect Smart - Demo Suite")
    print("="*60)
    
    settings = get_settings()
    print(f"\n📋 Configuratie:")
    print(f"   - Anthropic API: {'✅ Geconfigureerd' if settings.anthropic_api_key else '❌ Ontbreekt'}")
    print(f"   - Vapi API: {'✅ Geconfigureerd' if settings.vapi_private_key else '❌ Ontbreekt'}")
    print(f"   - WhatsApp: {'✅ Geconfigureerd' if settings.whatsapp_token else '⚠️ Niet ingesteld'}")
    
    # Voer demos uit
    await demo_planner()
    await demo_voice_info()
    await demo_restaurant_call_preview()
    
    # Browser demo is optioneel (opent daadwerkelijk een browser)
    print("\n" + "="*60)
    print("🌐 Browser demo overgeslagen (uncomment in code om te testen)")
    print("="*60)
    # await demo_browser_search()
    
    # Full flow demo
    # await demo_full_flow()
    
    print("\n" + "="*60)
    print("✅ Demo suite voltooid!")
    print("="*60)
    print("""
    Volgende stappen:
    
    1. Start de server:
       cd "/Users/c/Documents/agent belt voor reservering"
       source venv/bin/activate
       uvicorn src.main:app --reload
    
    2. Test de API:
       curl -X POST http://localhost:8000/task \\
         -H "Content-Type: application/json" \\
         -d '{"task": "Zoek restaurants in Amsterdam"}'
    
    3. Voor WhatsApp:
       - Maak een Meta Business account
       - Configureer de webhook URL
       - Vul WHATSAPP_TOKEN in .env
    
    4. Voor bellen:
       - Koop een telefoonnummer op Vapi dashboard
       - Test met /call endpoint
    """)


if __name__ == "__main__":
    asyncio.run(main())
