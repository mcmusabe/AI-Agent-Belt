# AI Agent Belt 🤖📞

Een autonoom AI-systeem dat opdrachten ontvangt via WhatsApp, websites kan bedienen, en zelfstandig kan bellen naar restaurants en bedrijven.

**Geïnspireerd door de OpenClaw demo van Alexander Klöpping bij Eva, maar dan 100x beter.**

## Wat kan het?

- 📱 **WhatsApp integratie** - Stuur een bericht en de AI regelt het
- 🌐 **Browser automatisering** - Maakt online reserveringen, koopt tickets
- 📞 **Telefonisch bellen** - Belt restaurants met natuurlijke Nederlandse stem
- 🧠 **Slim plannen** - Bepaalt zelf de beste aanpak
- 💾 **Geheugen** - Onthoudt je voorkeuren (coming soon)

## Quick Start

### 1. Installatie

```bash
# Clone of navigeer naar de project folder
cd "/Users/c/Documents/agent belt voor reservering"

# Activeer de virtual environment
source venv/bin/activate

# Dependencies zijn al geïnstalleerd!
```

### 2. Configuratie

De `.env` file is al aangemaakt met je API keys. Check of alles correct is:

```bash
cat .env
```

### 3. Start de server

```bash
uvicorn src.main:app --reload
```

De API draait nu op http://localhost:8000

### 4. Test het!

```bash
# Algemene taak
curl -X POST http://localhost:8000/task \
  -H "Content-Type: application/json" \
  -d '{"task": "Zoek de beste Italiaanse restaurants in Amsterdam"}'

# Check de API docs
open http://localhost:8000/docs
```

## API Endpoints

| Endpoint | Methode | Beschrijving |
|----------|---------|--------------|
| `/` | GET | Health check en info |
| `/task` | POST | Voer een algemene taak uit |
| `/reserve` | POST | Maak een reservering |
| `/call` | POST | Bel een restaurant |
| `/call/{id}/status` | GET | Status van een gesprek |
| `/call/{id}/transcript` | GET | Transcriptie van gesprek |
| `/browser/task` | POST | Directe browser opdracht |
| `/whatsapp/webhook` | GET/POST | WhatsApp integratie |

## Architectuur

```
┌─────────────────────────────────────────────────────────────┐
│                      INPUT KANALEN                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐                  │
│  │ WhatsApp │  │ Telegram │  │   API    │                  │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘                  │
└───────┼─────────────┼─────────────┼─────────────────────────┘
        │             │             │
        └─────────────┼─────────────┘
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                    ORCHESTRATOR                             │
│  ┌──────────────────────────────────────────────────────┐  │
│  │                   LangGraph                           │  │
│  │  ┌─────────┐  ┌─────────┐  ┌─────────┐              │  │
│  │  │ Analyze │──│  Route  │──│ Execute │              │  │
│  │  └─────────┘  └─────────┘  └─────────┘              │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                      │
        ┌─────────────┼─────────────┐
        ▼             ▼             ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│   Browser    │ │    Voice     │ │   Planner    │
│    Agent     │ │    Agent     │ │    Agent     │
│              │ │              │ │              │
│ Browser-Use  │ │   Vapi.ai    │ │    Claude    │
└──────────────┘ └──────────────┘ └──────────────┘
```

## Voorbeeld Gebruik

### Via WhatsApp (na configuratie)
```
Jij: "Reserveer een tafel voor 4 bij Ciel Bleu voor vrijdag 19:30"

AI: "📋 Plan gemaakt: Restaurant reservering bij Ciel Bleu
     
     ⏳ Ik ga nu:
     1. De website van Ciel Bleu bezoeken
     2. Een reservering maken voor 4 personen
     3. Vrijdag 19:30 selecteren
     
     📞 Als online niet lukt, bel ik ze."

AI: "✅ Reservering bevestigd!
     - Restaurant: Ciel Bleu
     - Datum: Vrijdag 7 februari
     - Tijd: 19:30
     - Personen: 4
     - Bevestigingsnummer: #CB2026-1234"
```

### Via API
```python
import requests

# Maak een reservering
response = requests.post("http://localhost:8000/reserve", json={
    "venue_name": "Restaurant De Kas",
    "date": "14 februari 2026",
    "time": "19:00",
    "party_size": 2,
    "name": "Jan Jansen",
    "email": "jan@example.com",
    "special_requests": "Graag een tafel bij het raam"
})

print(response.json())
```

## WhatsApp Configuratie

1. Maak een [Meta Business Account](https://business.facebook.com/)
2. Maak een WhatsApp Business App
3. Kopieer de tokens naar `.env`:
   ```
   WHATSAPP_TOKEN=je_token_hier
   WHATSAPP_PHONE_NUMBER_ID=je_nummer_id
   ```
4. Configureer de webhook URL: `https://jouw-server.com/whatsapp/webhook`

## Vapi Telefoonnummer

Om daadwerkelijk te kunnen bellen heb je een telefoonnummer nodig:

1. Ga naar [Vapi Dashboard](https://dashboard.vapi.ai/)
2. Koop een telefoonnummer
3. Test met de `/call` endpoint

## Development

### Project Structuur
```
agent-belt/
├── src/
│   ├── main.py              # FastAPI app
│   ├── config.py            # Settings
│   ├── agents/
│   │   ├── browser.py       # Browser-Use agent
│   │   ├── voice.py         # Vapi voice agent
│   │   └── planner.py       # Planning agent
│   ├── orchestrator/
│   │   └── graph.py         # LangGraph workflow
│   └── channels/
│       └── whatsapp.py      # WhatsApp integratie
├── demo.py                   # Demo script
├── requirements.txt
└── .env
```

### Testen

```bash
# Run de demo
python demo.py

# Of start de server en test via curl/Postman
uvicorn src.main:app --reload
```

## Tech Stack

- **Python 3.11** - Runtime
- **FastAPI** - Web framework
- **LangGraph** - Agent orchestratie
- **Browser-Use** - AI browser automatisering
- **Vapi.ai** - Voice AI voor telefoongesprekken
- **Claude (Anthropic)** - LLM
- **Playwright** - Browser engine

## TODO

- [ ] Supabase integratie voor geheugen
- [ ] Telegram channel
- [ ] Proactieve suggesties
- [ ] Multi-user support
- [ ] Betere error handling
- [ ] Unit tests

## License

MIT

## Credits

Geïnspireerd door de demo van Alexander Klöpping bij Eva Jinek, maar dan beter gebouwd met moderne tooling.
