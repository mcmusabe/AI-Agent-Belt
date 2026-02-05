"""
Telegram Channel - Integratie met Telegram Bot API.

Ontvangt berichten via polling en stuurt responses terug.
Veel simpeler dan WhatsApp - geen webhook of verificatie nodig!
"""
import asyncio
import logging
import tempfile
import os
from typing import Optional, Dict, Any
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from ..config import get_settings
from ..orchestrator.graph import process_request
from ..memory.supabase import get_memory_system
from ..agents.voice import VoiceAgent, _ended_reason_to_message
from ..scheduler.reminders import get_reminder_scheduler

# OpenAI voor Whisper (optioneel)
try:
    from openai import AsyncOpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

# Logging configuratie
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


class TelegramBot:
    """Telegram bot die berichten verwerkt via de AI orchestrator"""
    
    def __init__(self):
        self.settings = get_settings()
        self.application: Optional[Application] = None
        self.memory = None
        self.voice_agent = VoiceAgent()
        self._pending_calls: Dict[str, Dict[str, Any]] = {}  # call_id -> {chat_id, user_id, ...}
        self.openai_client = None
        
        # Initialize memory if Supabase is configured
        if self.settings.supabase_url and self.settings.supabase_anon_key:
            try:
                self.memory = get_memory_system()
                logger.info("💾 Memory system geactiveerd (Supabase)")
            except Exception as e:
                logger.warning(f"⚠️ Memory system niet beschikbaar: {e}")
        
        # Initialize OpenAI for Whisper if configured
        if OPENAI_AVAILABLE and self.settings.openai_api_key:
            try:
                self.openai_client = AsyncOpenAI(api_key=self.settings.openai_api_key)
                logger.info("🎤 Whisper speech-to-text geactiveerd (OpenAI)")
            except Exception as e:
                logger.warning(f"⚠️ OpenAI/Whisper niet beschikbaar: {e}")
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler voor /start command"""
        welcome_message = """
🤖 *Welkom bij Connect Smart!*

Ik ben je persoonlijke AI assistent. Ik kan:

📍 *Reserveringen maken*
"Reserveer een tafel voor 2 bij De Kas morgenavond om 19:00"

🔍 *Informatie zoeken*
"Wat zijn de beste Italiaanse restaurants in Amsterdam?"

📞 *Restaurants bellen* (binnenkort)
"Bel Restaurant X om te vragen of ze plek hebben"

💡 *Tip:* Gewoon in normale taal typen wat je wilt!
        """
        await update.message.reply_text(
            welcome_message,
            parse_mode="Markdown"
        )
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler voor /help command"""
        help_message = """
📚 *Hulp*

*Voorbeelden van wat je kunt vragen:*

🍽️ "Reserveer een tafel voor 4 bij Ciel Bleu"

📞 "Bel De Kas om te vragen of ze plek hebben"

🔎 "Wat zijn goede Italiaanse restaurants?"

🎤 Stuur een spraakbericht en ik versta je!

*Commands:*
/start - Welkomstbericht
/help - Deze hulp
/status - Systeem status
/contact - Beheer contacten
/herinner - Stel herinneringen in
/voorkeuren - Bekijk wat ik heb geleerd

*Tips:*
• Sla contacten op en typ "bel Jan" om te bellen
• Ik leer je voorkeuren automatisch
• Je krijgt transcripts na elk telefoongesprek

Typ gewoon wat je wilt! 🚀
        """
        await update.message.reply_text(
            help_message,
            parse_mode="Markdown"
        )
    
    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler voor /status command"""
        settings = self.settings
        
        status_message = f"""
⚙️ *Systeem Status*

✅ Telegram: Verbonden
{"✅" if settings.anthropic_api_key else "❌"} AI (Claude): {"Actief" if settings.anthropic_api_key else "Niet geconfigureerd"}
{"✅" if settings.vapi_private_key else "❌"} Voice (Vapi): {"Actief" if settings.vapi_private_key else "Niet geconfigureerd"}
{"✅" if self.memory else "❌"} Geheugen (Supabase): {"Actief" if self.memory else "Niet geconfigureerd"}

🤖 Bot: Connect Smart (@ai_agent_belt_bot)
📡 Versie: 1.2.0
        """
        await update.message.reply_text(
            status_message,
            parse_mode="Markdown"
        )
    
    async def contact_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Handler voor /contact command
        
        Gebruik:
        /contact - Toon alle contacten
        /contact add <naam> <telefoon> [categorie] - Voeg contact toe
        /contact zoek <naam> - Zoek een contact
        /contact del <naam> - Verwijder een contact
        """
        user_id = str(update.effective_user.id)
        args = context.args or []
        
        if not self.memory:
            await update.message.reply_text(
                "❌ Contacten zijn niet beschikbaar (geheugen niet geconfigureerd)."
            )
            return
        
        # Geen argumenten: toon alle contacten
        if not args:
            contacts = await self.memory.get_contacts(user_id)
            
            if not contacts:
                await update.message.reply_text(
                    "📇 *Contacten*\n\n"
                    "Je hebt nog geen contacten opgeslagen.\n\n"
                    "*Voeg een contact toe:*\n"
                    "`/contact add Jan Bakker +31612345678 restaurant`\n\n"
                    "*Categorieën:* restaurant, bedrijf, persoonlijk, overig",
                    parse_mode="Markdown"
                )
                return
            
            # Groepeer per categorie
            by_category: Dict[str, list] = {}
            for c in contacts:
                cat = c.get("category", "overig")
                if cat not in by_category:
                    by_category[cat] = []
                by_category[cat].append(c)
            
            message = "📇 *Contacten*\n\n"
            
            category_emojis = {
                "restaurant": "🍽️",
                "bedrijf": "🏢",
                "persoonlijk": "👤",
                "overig": "📌"
            }
            
            for cat, cat_contacts in by_category.items():
                emoji = category_emojis.get(cat, "📌")
                message += f"{emoji} *{cat.capitalize()}*\n"
                for c in cat_contacts:
                    name = c.get("name", "Onbekend")
                    phone = c.get("phone_number", "")
                    phone_str = f" - {phone}" if phone else ""
                    message += f"  • {name}{phone_str}\n"
                message += "\n"
            
            message += "_Tip: Typ gewoon \"bel Jan\" om te bellen_"
            
            await update.message.reply_text(message, parse_mode="Markdown")
            return
        
        action = args[0].lower()
        
        # Voeg contact toe
        if action == "add" and len(args) >= 3:
            name = args[1]
            phone = args[2] if len(args) > 2 else None
            category = args[3] if len(args) > 3 else "overig"
            
            # Normaliseer telefoonnummer
            if phone and not phone.startswith("+"):
                if phone.startswith("0"):
                    phone = "+31" + phone[1:]
            
            try:
                contact = await self.memory.add_contact(
                    telegram_id=user_id,
                    name=name,
                    phone_number=phone,
                    category=category
                )
                await update.message.reply_text(
                    f"✅ *Contact toegevoegd*\n\n"
                    f"👤 {name}\n"
                    f"📞 {phone or 'Geen nummer'}\n"
                    f"📁 {category}",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Error adding contact: {e}")
                await update.message.reply_text(f"❌ Kon contact niet toevoegen: {str(e)}")
            return
        
        # Zoek contact
        if action == "zoek" and len(args) >= 2:
            search_term = " ".join(args[1:])
            contacts = await self.memory.search_contacts(user_id, search_term)
            
            if not contacts:
                await update.message.reply_text(
                    f"🔍 Geen contacten gevonden voor '{search_term}'"
                )
                return
            
            message = f"🔍 *Resultaten voor '{search_term}'*\n\n"
            for c in contacts:
                name = c.get("name", "Onbekend")
                phone = c.get("phone_number", "Geen nummer")
                cat = c.get("category", "overig")
                message += f"• *{name}* - {phone} ({cat})\n"
            
            await update.message.reply_text(message, parse_mode="Markdown")
            return
        
        # Verwijder contact
        if action == "del" and len(args) >= 2:
            name = " ".join(args[1:])
            contact = await self.memory.get_contact_by_name(user_id, name)
            
            if not contact:
                await update.message.reply_text(
                    f"❌ Contact '{name}' niet gevonden."
                )
                return
            
            await self.memory.delete_contact(user_id, contact["id"])
            await update.message.reply_text(
                f"🗑️ Contact '{contact['name']}' verwijderd."
            )
            return
        
        # Help als commando niet herkend
        await update.message.reply_text(
            "📇 *Contact Commando's*\n\n"
            "`/contact` - Toon alle contacten\n"
            "`/contact add <naam> <telefoon> [categorie]` - Voeg toe\n"
            "`/contact zoek <naam>` - Zoek\n"
            "`/contact del <naam>` - Verwijder\n\n"
            "*Categorieën:* restaurant, bedrijf, persoonlijk, overig",
            parse_mode="Markdown"
        )
    
    async def preferences_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Handler voor /voorkeuren command - toon geleerde voorkeuren
        """
        user_id = str(update.effective_user.id)
        
        if not self.memory:
            await update.message.reply_text(
                "❌ Voorkeuren zijn niet beschikbaar (geheugen niet geconfigureerd)."
            )
            return
        
        try:
            # Haal user context op
            user_context = await self.memory.get_user_context(user_id)
            preferences = user_context.get("preferences", {})
            memories = user_context.get("memories", [])
            
            # Filter preference memories
            pref_memories = [m for m in memories if m.get("type") == "preference"]
            
            if not preferences and not pref_memories:
                await update.message.reply_text(
                    "🧠 *Jouw Voorkeuren*\n\n"
                    "Ik heb nog geen voorkeuren van je geleerd.\n\n"
                    "_Tip: Vertel me wat je lekker vindt! Bijvoorbeeld:_\n"
                    "• \"Ik hou van Italiaans eten\"\n"
                    "• \"Ik ben vegetarisch\"\n"
                    "• \"Ik maak liever 's avonds reserveringen\"",
                    parse_mode="Markdown"
                )
                return
            
            message = "🧠 *Jouw Voorkeuren*\n\n"
            
            if preferences:
                message += "*Opgeslagen voorkeuren:*\n"
                for key, value in preferences.items():
                    message += f"• {key}: {value}\n"
                message += "\n"
            
            if pref_memories:
                message += "*Wat ik heb geleerd:*\n"
                for mem in pref_memories[:10]:
                    message += f"• {mem.get('content', '')}\n"
            
            message += "\n_Ik leer automatisch van onze gesprekken!_"
            
            await update.message.reply_text(message, parse_mode="Markdown")
            
        except Exception as e:
            logger.error(f"Error getting preferences: {e}")
            await update.message.reply_text(
                "❌ Kon voorkeuren niet ophalen. Probeer later opnieuw."
            )
    
    async def reminder_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Handler voor /herinner command
        
        Gebruik:
        /herinner <tijd> <bericht>
        
        Voorbeelden:
        /herinner 15:00 Bel de dokter
        /herinner morgen 10:00 Meeting voorbereiden
        /herinner over 30 minuten Taart uit de oven
        """
        from datetime import datetime, timedelta
        import re
        import pytz
        
        user_id = str(update.effective_user.id)
        chat_id = str(update.effective_chat.id)
        args = context.args or []
        
        if not self.memory:
            await update.message.reply_text(
                "❌ Herinneringen zijn niet beschikbaar (geheugen niet geconfigureerd)."
            )
            return
        
        # Geen argumenten: toon huidige herinneringen
        if not args:
            reminders = await self.memory.get_user_reminders(user_id)
            
            if not reminders:
                await update.message.reply_text(
                    "⏰ *Herinneringen*\n\n"
                    "Je hebt geen actieve herinneringen.\n\n"
                    "*Maak een herinnering:*\n"
                    "`/herinner 15:00 Bel de dokter`\n"
                    "`/herinner morgen 10:00 Meeting`\n"
                    "`/herinner over 30 minuten Eten halen`",
                    parse_mode="Markdown"
                )
                return
            
            message = "⏰ *Jouw Herinneringen*\n\n"
            nl_tz = pytz.timezone('Europe/Amsterdam')
            
            for r in reminders:
                remind_time = datetime.fromisoformat(r["remind_at"].replace("Z", "+00:00"))
                remind_time = remind_time.astimezone(nl_tz)
                time_str = remind_time.strftime("%d-%m %H:%M")
                msg = r.get("message", "")[:50]
                repeat = r.get("repeat_interval", "")
                repeat_str = f" 🔁" if repeat else ""
                message += f"• {time_str} - {msg}{repeat_str}\n"
            
            message += "\n_Typ `/herinner` met tijd en bericht om toe te voegen_"
            
            await update.message.reply_text(message, parse_mode="Markdown")
            return
        
        # Parse tijd en bericht
        text = " ".join(args)
        nl_tz = pytz.timezone('Europe/Amsterdam')
        now = datetime.now(nl_tz)
        remind_time = None
        message_text = ""
        
        # Patronen voor tijdsaanduiding
        # "over X minuten/uur"
        over_match = re.match(r'over\s+(\d+)\s+(minuten?|uur|uren?|dagen?)', text, re.IGNORECASE)
        if over_match:
            amount = int(over_match.group(1))
            unit = over_match.group(2).lower()
            
            if 'minuut' in unit or 'minuten' in unit:
                remind_time = now + timedelta(minutes=amount)
            elif 'uur' in unit:
                remind_time = now + timedelta(hours=amount)
            elif 'dag' in unit:
                remind_time = now + timedelta(days=amount)
            
            message_text = text[over_match.end():].strip()
        
        # "morgen HH:MM"
        morgen_match = re.match(r'morgen\s+(\d{1,2}):(\d{2})', text, re.IGNORECASE)
        if morgen_match and not remind_time:
            hour = int(morgen_match.group(1))
            minute = int(morgen_match.group(2))
            remind_time = (now + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
            message_text = text[morgen_match.end():].strip()
        
        # "HH:MM" (vandaag of morgen als tijd al geweest is)
        time_match = re.match(r'(\d{1,2}):(\d{2})', text)
        if time_match and not remind_time:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2))
            remind_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            
            # Als de tijd al geweest is, plan voor morgen
            if remind_time <= now:
                remind_time += timedelta(days=1)
            
            message_text = text[time_match.end():].strip()
        
        if not remind_time or not message_text:
            await update.message.reply_text(
                "⚠️ *Formaat niet herkend*\n\n"
                "Voorbeelden:\n"
                "`/herinner 15:00 Bel de dokter`\n"
                "`/herinner morgen 10:00 Meeting`\n"
                "`/herinner over 30 minuten Taart uit de oven`",
                parse_mode="Markdown"
            )
            return
        
        try:
            # Sla herinnering op
            await self.memory.add_reminder(
                telegram_id=user_id,
                message=message_text,
                remind_at=remind_time,
                telegram_chat_id=chat_id
            )
            
            time_str = remind_time.strftime("%d-%m-%Y om %H:%M")
            
            await update.message.reply_text(
                f"✅ *Herinnering ingesteld*\n\n"
                f"📝 {message_text}\n"
                f"⏰ {time_str}",
                parse_mode="Markdown"
            )
            
        except Exception as e:
            logger.error(f"Error adding reminder: {e}")
            await update.message.reply_text(
                "❌ Kon herinnering niet opslaan. Probeer later opnieuw."
            )
    
    async def poll_call_transcript(self, call_id: str, chat_id: int, user_id: str):
        """
        Poll voor call transcript en stuur naar gebruiker wanneer klaar.
        
        Args:
            call_id: Vapi call ID
            chat_id: Telegram chat ID om resultaat naar te sturen
            user_id: User ID voor memory
        """
        max_attempts = 60  # Max 5 minuten (60 * 5 sec)
        attempt = 0
        
        while attempt < max_attempts:
            await asyncio.sleep(5)  # Wacht 5 seconden tussen polls
            attempt += 1
            
            try:
                result = await self.voice_agent.get_call_status(call_id)
                
                if not result.get("success"):
                    logger.warning(f"⚠️ Kon call status niet ophalen: {result.get('error')}")
                    continue
                
                call_data = result.get("call", {})
                status = call_data.get("status", "")
                
                logger.info(f"📞 Call {call_id} status: {status} (attempt {attempt})")
                
                # Als call beëindigd is
                if status in ["ended", "failed", "busy", "no-answer"]:
                    # Haal transcript + endedReason op (zodat we altijd kunnen uitleggen waarom)
                    transcript_result = await self.voice_agent.get_call_transcript(call_id)
                    
                    transcript = transcript_result.get("transcript", "")
                    summary = transcript_result.get("summary", "")
                    duration = transcript_result.get("duration", 0)
                    ended_reason = transcript_result.get("endedReason", "")
                    reason_message = _ended_reason_to_message(ended_reason)
                    if ended_reason:
                        logger.info("📞 Call %s ended: reason=%s duration=%ss", call_id, ended_reason, duration)
                    
                    if status == "ended":
                        if transcript or summary:
                            message = f"📞 *Gesprek beëindigd*\n\n"
                            message += f"⏱️ Duur: {int(duration)} seconden\n\n"
                            if int(duration) == 0 and reason_message:
                                message += f"ℹ️ {reason_message}\n\n"
                            if summary:
                                message += f"📝 *Samenvatting:*\n{summary}\n\n"
                            if transcript:
                                if len(transcript) > 3000:
                                    transcript = transcript[:3000] + "...\n(transcript ingekort)"
                                message += f"💬 *Transcript:*\n{transcript}"
                        else:
                            message = f"📞 *Gesprek beëindigd* (⏱️ {int(duration)}s)\n\n"
                            message += f"Geen transcript beschikbaar.\n\n"
                            if reason_message:
                                message += f"ℹ️ {reason_message}"
                    elif status == "failed":
                        message = f"❌ *Gesprek mislukt*\n\n{reason_message}"
                    elif status == "busy":
                        message = f"📞 *Lijn bezet*\n\n{reason_message}"
                    elif status == "no-answer":
                        message = f"📞 *Niet opgenomen*\n\n{reason_message}"
                    
                    # Stuur naar Telegram
                    if self.application and self.application.bot:
                        await self.application.bot.send_message(
                            chat_id=chat_id,
                            text=message,
                            parse_mode="Markdown"
                        )
                        
                        # Sla transcript op in memory
                        if self.memory and transcript:
                            try:
                                conv = await self.memory.get_active_conversation(user_id)
                                if conv:
                                    await self.memory.add_message(
                                        conversation_id=conv["id"],
                                        role="assistant",
                                        content=f"[Call Transcript]\n{summary or transcript[:500]}",
                                        metadata={"call_id": call_id, "duration": duration}
                                    )
                            except Exception as e:
                                logger.warning(f"⚠️ Kon transcript niet opslaan: {e}")
                    
                    # Verwijder uit pending calls
                    self._pending_calls.pop(call_id, None)
                    return
                    
            except Exception as e:
                logger.error(f"❌ Error bij poll call transcript: {e}")
        
        # Timeout bereikt
        if self.application and self.application.bot:
            await self.application.bot.send_message(
                chat_id=chat_id,
                text="⚠️ *Timeout*: Kon de gespreksstatus niet ophalen binnen 5 minuten.",
                parse_mode="Markdown"
            )
        self._pending_calls.pop(call_id, None)
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler voor normale tekstberichten"""
        user = update.effective_user
        message_text = update.message.text
        user_id = str(user.id)
        chat_id = update.effective_chat.id
        
        logger.info(f"📱 Bericht van {user.first_name} ({user_id}): {message_text}")
        
        # Stuur typing indicator
        await update.message.chat.send_action("typing")
        
        # Sla gebruiker en bericht op in memory
        conversation_id = None
        if self.memory:
            try:
                # Zorg dat user bestaat
                await self.memory.get_or_create_user(
                    telegram_id=user_id,
                    telegram_username=user.username,
                    first_name=user.first_name,
                    last_name=user.last_name
                )
                
                # Haal of start conversatie
                conv = await self.memory.get_active_conversation(user_id)
                if not conv:
                    conv = await self.memory.start_conversation(user_id)
                conversation_id = conv["id"]
                
                # Sla user bericht op
                await self.memory.add_message(
                    conversation_id=conversation_id,
                    role="user",
                    content=message_text
                )
            except Exception as e:
                logger.warning(f"⚠️ Memory error: {e}")
        
        try:
            # Verwerk via de orchestrator
            result = await process_request(
                user_message=message_text,
                user_id=user_id
            )
            
            # Haal response en call_id uit het resultaat
            response = result.get("response", "Er ging iets mis.")
            call_id = result.get("call_id")
            
            # Sla assistant response op in memory
            if self.memory and conversation_id:
                try:
                    await self.memory.add_message(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=response
                    )
                except Exception as e:
                    logger.warning(f"⚠️ Memory save error: {e}")
            
            # Stuur response terug
            await update.message.reply_text(response)
            
            logger.info(f"📤 Response naar {user.first_name}: {response[:100]}...")
            
            # Als er een call gestart is, start transcript polling in background
            if call_id and call_id != "onbekend":
                logger.info(f"📞 Start transcript polling voor call {call_id}")
                self._pending_calls[call_id] = {
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "started_at": asyncio.get_event_loop().time()
                }
                # Start background task voor transcript polling
                asyncio.create_task(
                    self.poll_call_transcript(call_id, chat_id, user_id)
                )
            
        except Exception as e:
            logger.error(f"❌ Error bij verwerken bericht: {e}")
            await update.message.reply_text(
                "Sorry, er ging iets mis bij het verwerken van je verzoek. "
                "Probeer het opnieuw of typ /help voor hulp."
            )
    
    async def handle_voice_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler voor spraakberichten - transcribeer en verwerk"""
        user = update.effective_user
        user_id = str(user.id)
        chat_id = update.effective_chat.id
        
        logger.info(f"🎤 Spraakbericht van {user.first_name} ({user_id})")
        
        # Check of Whisper beschikbaar is
        if not self.openai_client:
            await update.message.reply_text(
                "❌ Spraakberichten zijn niet beschikbaar. "
                "Typ je bericht of configureer OpenAI API key."
            )
            return
        
        # Stuur processing indicator
        await update.message.chat.send_action("typing")
        
        voice = update.message.voice or update.message.audio
        if not voice:
            return
        
        try:
            # Download voice file
            file = await context.bot.get_file(voice.file_id)
            
            # Maak tijdelijk bestand
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
                tmp_path = tmp.name
            
            # Download naar tijdelijk bestand
            await file.download_to_drive(tmp_path)
            
            # Transcribeer met Whisper
            with open(tmp_path, "rb") as audio_file:
                transcript = await self.openai_client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    language="nl"  # Nederlands
                )
            
            # Verwijder tijdelijk bestand
            os.unlink(tmp_path)
            
            message_text = transcript.text
            
            if not message_text.strip():
                await update.message.reply_text(
                    "🤔 Ik kon je niet verstaan. Kun je het opnieuw proberen?"
                )
                return
            
            # Bevestig wat we hebben gehoord
            await update.message.reply_text(
                f"🎤 _\"{message_text}\"_",
                parse_mode="Markdown"
            )
            
            logger.info(f"📝 Getranscribeerd: {message_text}")
            
            # Sla in memory op
            conversation_id = None
            if self.memory:
                try:
                    await self.memory.get_or_create_user(
                        telegram_id=user_id,
                        telegram_username=user.username,
                        first_name=user.first_name,
                        last_name=user.last_name
                    )
                    
                    conv = await self.memory.get_active_conversation(user_id)
                    if not conv:
                        conv = await self.memory.start_conversation(user_id)
                    conversation_id = conv["id"]
                    
                    await self.memory.add_message(
                        conversation_id=conversation_id,
                        role="user",
                        content=f"[Voice] {message_text}",
                        metadata={"type": "voice", "duration": voice.duration}
                    )
                except Exception as e:
                    logger.warning(f"⚠️ Memory error: {e}")
            
            # Verwerk het getranscribeerde bericht
            await update.message.chat.send_action("typing")
            
            result = await process_request(
                user_message=message_text,
                user_id=user_id
            )
            
            response = result.get("response", "Er ging iets mis.")
            call_id = result.get("call_id")
            
            # Sla response op
            if self.memory and conversation_id:
                try:
                    await self.memory.add_message(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=response
                    )
                except Exception as e:
                    logger.warning(f"⚠️ Memory save error: {e}")
            
            await update.message.reply_text(response)
            
            # Start call transcript polling indien nodig
            if call_id and call_id != "onbekend":
                logger.info(f"📞 Start transcript polling voor call {call_id}")
                self._pending_calls[call_id] = {
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "started_at": asyncio.get_event_loop().time()
                }
                asyncio.create_task(
                    self.poll_call_transcript(call_id, chat_id, user_id)
                )
            
        except Exception as e:
            logger.error(f"❌ Error bij verwerken spraakbericht: {e}")
            await update.message.reply_text(
                "Sorry, er ging iets mis bij het verwerken van je spraakbericht. "
                "Probeer het opnieuw of typ je bericht."
            )
            
            # Cleanup temp file if exists
            if 'tmp_path' in locals() and os.path.exists(tmp_path):
                os.unlink(tmp_path)
    
    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler voor errors"""
        logger.error(f"Error: {context.error}")
        
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "Er is een fout opgetreden. Probeer het later opnieuw."
            )
    
    def build_application(self) -> Application:
        """Bouw de Telegram application"""
        if not self.settings.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN niet geconfigureerd in .env")
        
        # Maak application
        self.application = (
            Application.builder()
            .token(self.settings.telegram_bot_token)
            .build()
        )
        
        # Voeg handlers toe
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("status", self.status_command))
        self.application.add_handler(CommandHandler("contact", self.contact_command))
        self.application.add_handler(CommandHandler("voorkeuren", self.preferences_command))
        self.application.add_handler(CommandHandler("herinner", self.reminder_command))
        
        # Message handler voor alle tekst berichten
        self.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message)
        )
        
        # Voice message handler
        self.application.add_handler(
            MessageHandler(filters.VOICE | filters.AUDIO, self.handle_voice_message)
        )
        
        # Error handler
        self.application.add_error_handler(self.error_handler)
        
        return self.application
    
    async def _send_reminder_message(self, chat_id: str, message: str):
        """Callback voor het versturen van herinneringen via de bot"""
        if self.application and self.application.bot:
            try:
                await self.application.bot.send_message(
                    chat_id=int(chat_id),
                    text=message,
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"❌ Kon herinnering niet versturen naar {chat_id}: {e}")
    
    async def run_polling(self):
        """Start de bot met polling"""
        app = self.build_application()
        
        logger.info("🤖 Telegram bot gestart!")
        logger.info("📱 Bot: Connect Smart (@ai_agent_belt_bot)")
        
        # Initialize en start polling
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        
        # Start reminder scheduler
        scheduler = get_reminder_scheduler()
        scheduler.set_message_callback(self._send_reminder_message)
        scheduler.start()
        logger.info("⏰ Reminder scheduler geactiveerd")
        
        # Houd de bot draaiende
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            scheduler.stop()
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
    
    def run(self):
        """Start de bot (blocking)"""
        app = self.build_application()
        logger.info("🤖 Telegram bot gestart!")
        logger.info("📱 Bot: Connect Smart (@ai_agent_belt_bot)")
        app.run_polling(drop_pending_updates=True)


# Singleton instance
_bot: Optional[TelegramBot] = None


def get_telegram_bot() -> TelegramBot:
    """Get or create the Telegram bot instance"""
    global _bot
    if _bot is None:
        _bot = TelegramBot()
    return _bot


async def start_telegram_bot():
    """Start de Telegram bot als achtergrondtaak"""
    bot = get_telegram_bot()
    await bot.run_polling()


# Voor directe uitvoering
if __name__ == "__main__":
    bot = TelegramBot()
    bot.run()
