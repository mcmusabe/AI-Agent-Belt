"""
Telegram Channel - Integratie met Telegram Bot API.

Ontvangt berichten via polling en stuurt responses terug.
Veel simpeler dan WhatsApp - geen webhook of verificatie nodig!
"""
import asyncio
import logging
import re as _re_module
import tempfile
import os
from dataclasses import dataclass, field
from enum import Enum
from uuid import uuid4
from typing import Optional, Dict, Any, List
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
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
from ..tools.google_calendar import list_events, get_free_slots
from ..tools.sms import send_sms
from ..tools.gmail import send_email


# ── Wizard data structuren ──────────────────────────────────────────

class WizardType(str, Enum):
    BELLEN = "bellen"
    SMS = "sms"
    MAIL = "mail"
    AGENDA = "agenda"


class WizardStep(str, Enum):
    SELECT_CONTACT = "select_contact"
    ENTER_NUMBER = "enter_number"
    ENTER_EMAIL = "enter_email"
    ENTER_MESSAGE = "enter_message"
    ENTER_SUBJECT = "enter_subject"
    ENTER_TITLE = "enter_title"
    ENTER_DATETIME = "enter_datetime"
    CONFIRM = "confirm"


# Stappen per wizard type
WIZARD_FLOWS: Dict[WizardType, list] = {
    WizardType.BELLEN: [
        WizardStep.SELECT_CONTACT,
        WizardStep.ENTER_MESSAGE,
        WizardStep.CONFIRM,
    ],
    WizardType.SMS: [
        WizardStep.SELECT_CONTACT,
        WizardStep.ENTER_MESSAGE,
        WizardStep.CONFIRM,
    ],
    WizardType.MAIL: [
        WizardStep.SELECT_CONTACT,
        WizardStep.ENTER_SUBJECT,
        WizardStep.ENTER_MESSAGE,
        WizardStep.CONFIRM,
    ],
    WizardType.AGENDA: [
        WizardStep.ENTER_TITLE,
        WizardStep.ENTER_DATETIME,
        WizardStep.CONFIRM,
    ],
}


@dataclass
class WizardState:
    """State voor een actieve wizard conversatie"""
    wizard_type: WizardType
    current_step: WizardStep
    user_id: str
    chat_id: int
    started_at: float
    # Verzamelde data
    contact_name: Optional[str] = None
    phone_number: Optional[str] = None
    email_address: Optional[str] = None
    message_body: Optional[str] = None
    subject: Optional[str] = None
    event_title: Optional[str] = None
    event_datetime: Optional[str] = None


WIZARD_TIMEOUT = 300  # 5 minuten

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
        self._pending_confirmations: Dict[str, Dict[str, Any]] = {}  # token -> {user_id, chat_id, message}
        self._pending_wizards: Dict[str, WizardState] = {}  # user_id -> WizardState
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
    
    @staticmethod
    def _get_main_menu_keyboard() -> ReplyKeyboardMarkup:
        """Persistent menu keyboard onderaan het scherm"""
        keyboard = [
            [KeyboardButton("📞 Bellen"), KeyboardButton("📱 SMS"), KeyboardButton("📧 Mail")],
            [KeyboardButton("📅 Agenda"), KeyboardButton("🕐 Vrije Tijd"), KeyboardButton("📊 Overzicht")],
            [KeyboardButton("📇 Contacten"), KeyboardButton("❓ Help")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler voor /start command"""
        welcome_message = """
🤖 *Welkom bij Connect Smart!*

Ik ben je persoonlijke AI assistent. Kies een actie uit het menu hieronder, of typ gewoon wat je wilt!

📞 *Bellen* — Ik bel namens jou
📱 *SMS* — Stuur een berichtje
📧 *Mail* — Verstuur een e-mail
📅 *Agenda* — Plan een afspraak
🕐 *Vrije Tijd* — Check wanneer je vrij bent
📊 *Overzicht* — Weekoverzicht van alles
📇 *Contacten* — Beheer je contacten

💡 *Tip:* Je kunt ook gewoon typen: "bel mam" of "sms Jan dat ik later kom"
        """
        await update.message.reply_text(
            welcome_message,
            parse_mode="Markdown",
            reply_markup=self._get_main_menu_keyboard(),
        )

    async def menu_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler voor /menu command — toon het hoofdmenu"""
        await update.message.reply_text(
            "📋 *Hoofdmenu* — Kies een actie:",
            parse_mode="Markdown",
            reply_markup=self._get_main_menu_keyboard(),
        )
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler voor /help command"""
        help_message = """
📚 *Hulp*

*Voorbeelden van wat je kunt vragen:*

📞 "Bel De Kas om te vragen of ze plek hebben"
📱 "SMS Jan dat ik later kom"
📧 "Mail jan@example.com de offerte"
📅 "Zet morgen om 10:00 een meeting in mijn agenda"
🍽️ "Reserveer een tafel voor 4 bij Ciel Bleu"
🔎 "Wat zijn goede Italiaanse restaurants?"
🎤 Stuur een spraakbericht en ik versta je!

*Commands:*
/start - Welkomstbericht
/help - Deze hulp
/status - Systeem status
/contact - Beheer contacten
/herinner - Stel herinneringen in
/voorkeuren - Bekijk wat ik heb geleerd
/overzicht - Weekoverzicht (calls, sms, mails)
/vrijetijd - Vrije tijdsloten in je agenda

*Groeps-SMS/mail:*
• "sms iedereen uit persoonlijk dat het feest morgen is"
• "mail iedereen uit bedrijf de nieuwe prijslijst"

*Tips:*
• Sla contacten op: `/contact add Jan +31612345678 persoonlijk`
• Typ dan "bel Jan", "sms Jan" of "mail Jan"
• Na elk gesprek krijg je suggesties (contact opslaan, herinnering, SMS)
• Ik leer je voorkeuren automatisch

Typ gewoon wat je wilt!
        """
        await update.message.reply_text(
            help_message,
            parse_mode="Markdown"
        )
    
    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler voor /status command"""
        settings = self.settings
        
        sms_ok = bool(settings.twilio_sms_number or settings.twilio_messaging_service_sid)
        gmail_ok = bool(settings.google_refresh_token and settings.gmail_from_email)
        calendar_ok = bool(settings.google_refresh_token)

        status_message = f"""
⚙️ *Systeem Status*

✅ Telegram: Verbonden
{"✅" if settings.anthropic_api_key else "❌"} AI (Claude): {"Actief" if settings.anthropic_api_key else "Niet geconfigureerd"}
{"✅" if settings.vapi_private_key else "❌"} Voice (Vapi): {"Actief" if settings.vapi_private_key else "Niet geconfigureerd"}
{"✅" if sms_ok else "❌"} SMS (Twilio): {"Actief" if sms_ok else "Niet geconfigureerd"}
{"✅" if gmail_ok else "❌"} E-mail (Gmail): {"Actief" if gmail_ok else "Niet geconfigureerd"}
{"✅" if calendar_ok else "❌"} Agenda (Google): {"Actief" if calendar_ok else "Niet geconfigureerd"}
{"✅" if self.memory else "❌"} Geheugen (Supabase): {"Actief" if self.memory else "Niet geconfigureerd"}

🤖 Bot: Connect Smart (@ai\_agent\_belt\_bot)
📡 Versie: 2.0.0
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
                    "`/contact add Jan +31612345678 jan@email.nl persoonlijk`\n\n"
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
                    email = c.get("email", "")
                    details = []
                    if phone:
                        details.append(phone)
                    if email:
                        details.append(email)
                    detail_str = f" - {', '.join(details)}" if details else ""
                    message += f"  • {name}{detail_str}\n"
                message += "\n"
            
            message += "_Tip: Typ gewoon \"bel Jan\" om te bellen_"
            
            await update.message.reply_text(message, parse_mode="Markdown")
            return
        
        action = args[0].lower()
        
        # Voeg contact toe
        # Syntax: /contact add <naam> <telefoon> [email] [categorie]
        if action == "add" and len(args) >= 3:
            name = args[1]
            phone = None
            email = None
            category = "overig"

            # Parse overige args: detect telefoon, email, categorie
            for arg in args[2:]:
                if "@" in arg:
                    email = arg
                elif arg.startswith("+") or arg[0].isdigit():
                    phone = arg
                elif arg.lower() in ("restaurant", "bedrijf", "persoonlijk", "overig"):
                    category = arg.lower()

            # Normaliseer telefoonnummer
            if phone and not phone.startswith("+"):
                if phone.startswith("0"):
                    phone = "+31" + phone[1:]

            try:
                contact = await self.memory.add_contact(
                    telegram_id=user_id,
                    name=name,
                    phone_number=phone,
                    email=email,
                    category=category,
                )
                lines = [
                    f"✅ *Contact toegevoegd*\n",
                    f"👤 {name}",
                ]
                if phone:
                    lines.append(f"📞 {phone}")
                if email:
                    lines.append(f"📧 {email}")
                lines.append(f"📁 {category}")
                await update.message.reply_text(
                    "\n".join(lines),
                    parse_mode="Markdown",
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
            "`/contact add <naam> <telefoon> [email] [categorie]` - Voeg toe\n"
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

    async def overzicht_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Handler voor /overzicht command — weekoverzicht dashboard.
        Toont alle calls, SMS, mails en herinneringen van de afgelopen 7 dagen.
        """
        user_id = str(update.effective_user.id)

        if not self.memory:
            await update.message.reply_text(
                "❌ Overzicht niet beschikbaar (geheugen niet geconfigureerd)."
            )
            return

        await update.message.chat.send_action("typing")

        try:
            activity = await self.memory.get_weekly_activity(user_id)

            calls = activity.get("calls", [])
            sms_msgs = activity.get("sms_messages", [])
            emails = activity.get("emails", [])
            reminders = activity.get("reminders", [])

            total = len(calls) + len(sms_msgs) + len(emails)

            message = "📊 *Weekoverzicht* (afgelopen 7 dagen)\n"
            message += "━━━━━━━━━━━━━━━━━━━━━\n\n"

            # Calls
            message += f"📞 *Gesprekken:* {len(calls)}\n"
            for c in calls[:5]:
                phone = c.get("phone_number", "?")
                status = c.get("status", "?")
                duration = c.get("duration_seconds")
                dur_str = f" ({duration}s)" if duration else ""
                status_emoji = "✅" if status == "ended" else "❌"
                summary_snippet = ""
                if c.get("summary"):
                    summary_snippet = f"\n   └ _{c['summary'][:60]}…_"
                message += f"  {status_emoji} {phone}{dur_str}{summary_snippet}\n"
            if len(calls) > 5:
                message += f"  _…en {len(calls) - 5} meer_\n"
            message += "\n"

            # SMS
            message += f"📱 *SMS berichten:* {len(sms_msgs)}\n"
            for s in sms_msgs[:3]:
                content = s.get("content", "")[:60]
                message += f"  • {content}…\n"
            if len(sms_msgs) > 3:
                message += f"  _…en {len(sms_msgs) - 3} meer_\n"
            message += "\n"

            # Emails
            message += f"📧 *E-mails:* {len(emails)}\n"
            for e in emails[:3]:
                content = e.get("content", "")[:60]
                message += f"  • {content}…\n"
            if len(emails) > 3:
                message += f"  _…en {len(emails) - 3} meer_\n"
            message += "\n"

            # Reminders
            active_reminders = [r for r in reminders if not r.get("is_sent")]
            sent_reminders = [r for r in reminders if r.get("is_sent")]
            message += f"⏰ *Herinneringen:* {len(sent_reminders)} afgelopen, {len(active_reminders)} actief\n"
            for r in active_reminders[:3]:
                msg = r.get("message", "?")[:40]
                message += f"  • {msg}\n"
            message += "\n"

            # Agenda vandaag + morgen
            try:
                from datetime import datetime, timedelta
                import pytz
                nl_tz = pytz.timezone("Europe/Amsterdam")
                now = datetime.now(nl_tz)
                tomorrow_end = (now + timedelta(days=2)).replace(hour=0, minute=0, second=0)

                events = list_events(
                    time_min=now.isoformat(),
                    time_max=tomorrow_end.isoformat(),
                )
                if events:
                    message += f"📅 *Komende afspraken:* {len(events)}\n"
                    for ev in events[:5]:
                        summary = ev.get("summary", "Geen titel")
                        start = ev.get("start", {}).get("dateTime", ev.get("start", {}).get("date", ""))
                        if "T" in start:
                            try:
                                dt = datetime.fromisoformat(start)
                                time_str = dt.strftime("%d-%m %H:%M")
                            except ValueError:
                                time_str = start
                        else:
                            time_str = start
                        message += f"  • {time_str} — {summary}\n"
                    message += "\n"
            except Exception:
                pass

            message += "━━━━━━━━━━━━━━━━━━━━━\n"
            message += f"📈 *Totaal acties:* {total}"

            await update.message.reply_text(message, parse_mode="Markdown")

        except Exception as e:
            logger.error(f"Error bij weekoverzicht: {e}")
            await update.message.reply_text(
                "❌ Kon weekoverzicht niet ophalen. Probeer later opnieuw."
            )

    async def vrije_tijd_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Handler voor /vrijetijd command en '🕐 Vrije Tijd' menu button.
        Toont vrije tijdsloten voor vandaag en morgen.
        """
        from datetime import datetime, timedelta
        import pytz

        await update.message.chat.send_action("typing")

        nl_tz = pytz.timezone("Europe/Amsterdam")
        now = datetime.now(nl_tz)
        today_str = now.strftime("%Y-%m-%d")
        tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        overmorgen_str = (now + timedelta(days=2)).strftime("%Y-%m-%d")

        message = "🕐 *Vrije tijdsloten*\n━━━━━━━━━━━━━━━━━━━━━\n\n"

        days_to_check = [
            ("Vandaag", today_str, now.strftime("%A %d-%m")),
            ("Morgen", tomorrow_str, (now + timedelta(days=1)).strftime("%A %d-%m")),
            ("Overmorgen", overmorgen_str, (now + timedelta(days=2)).strftime("%A %d-%m")),
        ]

        # Nederlandse dagnamen
        dag_map = {
            "Monday": "Maandag", "Tuesday": "Dinsdag", "Wednesday": "Woensdag",
            "Thursday": "Donderdag", "Friday": "Vrijdag", "Saturday": "Zaterdag",
            "Sunday": "Zondag",
        }

        try:
            for label, date_str, day_display in days_to_check:
                # Vertaal dagnaam
                for eng, nl in dag_map.items():
                    day_display = day_display.replace(eng, nl)

                try:
                    slots = get_free_slots(date_str)
                    message += f"📅 *{label}* ({day_display})\n"

                    if not slots:
                        message += "  ⚠️ Geen vrije slots gevonden\n"
                    else:
                        for slot in slots:
                            start = slot["start"]
                            end = slot["end"]
                            # Bereken duur
                            s_h, s_m = map(int, start.split(":"))
                            e_h, e_m = map(int, end.split(":"))
                            dur_min = (e_h * 60 + e_m) - (s_h * 60 + s_m)
                            if dur_min >= 60:
                                dur_str = f"{dur_min // 60}u{dur_min % 60:02d}" if dur_min % 60 else f"{dur_min // 60}u"
                            else:
                                dur_str = f"{dur_min}min"
                            message += f"  🟢 {start} – {end} ({dur_str})\n"
                    message += "\n"
                except Exception as e:
                    message += f"📅 *{label}*: ❌ Kon niet ophalen\n\n"

            message += "💡 _Typ \"📅 Agenda\" om een afspraak in te plannen_"

            await update.message.reply_text(message, parse_mode="Markdown")

        except Exception as e:
            logger.error(f"Error bij vrije tijd check: {e}")
            await update.message.reply_text(
                "❌ Kon agenda niet checken. Is Google Calendar gekoppeld?\n"
                "Check /status voor de configuratie."
            )

    # ── Wizard management ─────────────────────────────────────────────

    def _start_wizard(self, user_id: str, chat_id: int, wizard_type: WizardType) -> WizardState:
        """Start een nieuwe wizard voor een gebruiker (vervangt eventueel bestaande)"""
        first_step = WIZARD_FLOWS[wizard_type][0]
        state = WizardState(
            wizard_type=wizard_type,
            current_step=first_step,
            user_id=user_id,
            chat_id=chat_id,
            started_at=asyncio.get_event_loop().time(),
        )
        self._pending_wizards[user_id] = state
        return state

    def _get_wizard(self, user_id: str) -> Optional[WizardState]:
        """Haal actieve wizard op; verwijder als timeout verstreken"""
        wizard = self._pending_wizards.get(user_id)
        if wizard:
            elapsed = asyncio.get_event_loop().time() - wizard.started_at
            if elapsed > WIZARD_TIMEOUT:
                self._clear_wizard(user_id)
                return None
        return wizard

    def _clear_wizard(self, user_id: str):
        """Verwijder wizard state voor gebruiker"""
        self._pending_wizards.pop(user_id, None)

    def _advance_wizard(self, user_id: str) -> Optional[WizardStep]:
        """Ga naar de volgende stap; return None als wizard klaar is"""
        wizard = self._get_wizard(user_id)
        if not wizard:
            return None
        flow = WIZARD_FLOWS[wizard.wizard_type]
        try:
            idx = flow.index(wizard.current_step)
        except ValueError:
            # Huidige stap zit niet in de flow (bijv. ENTER_NUMBER is een tussenstap).
            # Zoek de logische volgende stap op basis van welke data we al hebben.
            if wizard.current_step in (WizardStep.ENTER_NUMBER, WizardStep.ENTER_EMAIL):
                # Na nummer/email invoer → ga naar de stap NA SELECT_CONTACT
                try:
                    sc_idx = flow.index(WizardStep.SELECT_CONTACT)
                    if sc_idx + 1 < len(flow):
                        wizard.current_step = flow[sc_idx + 1]
                        return wizard.current_step
                except ValueError:
                    pass
            # Fallback: ga naar de eerste stap die we nog niet gehad hebben
            return None
        if idx + 1 < len(flow):
            wizard.current_step = flow[idx + 1]
            return wizard.current_step
        return None

    # ── Wizard UI helpers ──────────────────────────────────────────────

    async def _build_contact_keyboard(
        self, user_id: str, wizard_type: WizardType, max_contacts: int = 6
    ) -> InlineKeyboardMarkup:
        """Bouw inline keyboard met contacten van de gebruiker"""
        buttons: List[List[InlineKeyboardButton]] = []
        wt = wizard_type.value

        if self.memory:
            try:
                contacts = await self.memory.get_contacts(user_id)

                # Filter: mail-wizard toont alleen contacten met email
                if wizard_type == WizardType.MAIL:
                    contacts = [c for c in contacts if c.get("email")]
                else:
                    contacts = [c for c in contacts if c.get("phone_number")]

                for contact in contacts[:max_contacts]:
                    name = contact.get("name", "?")[:15]
                    cid = str(contact.get("id", ""))[:20]
                    buttons.append([
                        InlineKeyboardButton(
                            f"👤 {name}",
                            callback_data=f"wz:{wt}:ct:{cid}",
                        )
                    ])
            except Exception:
                pass

        # Handmatig invoeren
        if wizard_type == WizardType.MAIL:
            manual_label = "📝 Ander e-mailadres…"
        else:
            manual_label = "📝 Ander nummer…"
        buttons.append([InlineKeyboardButton(manual_label, callback_data=f"wz:{wt}:ct:manual")])
        buttons.append([InlineKeyboardButton("❌ Annuleren", callback_data="wz:cancel")])
        return InlineKeyboardMarkup(buttons)

    def _build_confirm_keyboard(self, wizard_type: WizardType) -> InlineKeyboardMarkup:
        """Bevestigings-knoppen voor de laatste stap"""
        wt = wizard_type.value
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Ja, doen", callback_data=f"wz:{wt}:ok:yes"),
                InlineKeyboardButton("❌ Annuleren", callback_data="wz:cancel"),
            ]
        ])

    def _build_datetime_keyboard(self, wizard_type: WizardType) -> InlineKeyboardMarkup:
        """Snelle datum/tijd opties voor agenda wizard"""
        wt = wizard_type.value
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Morgen 09:00", callback_data=f"wz:{wt}:dt:m09"),
                InlineKeyboardButton("Morgen 14:00", callback_data=f"wz:{wt}:dt:m14"),
            ],
            [
                InlineKeyboardButton("Overmorgen 10:00", callback_data=f"wz:{wt}:dt:o10"),
                InlineKeyboardButton("Overmorgen 15:00", callback_data=f"wz:{wt}:dt:o15"),
            ],
            [InlineKeyboardButton("📝 Andere tijd…", callback_data=f"wz:{wt}:dt:custom")],
            [InlineKeyboardButton("❌ Annuleren", callback_data="wz:cancel")],
        ])

    async def _render_wizard_step(self, update_or_query, wizard: WizardState, *, edit: bool = False):
        """Render de huidige wizard stap naar de gebruiker"""
        step = wizard.current_step
        wt = wizard.wizard_type

        # Emoji per type
        emoji_map = {
            WizardType.BELLEN: "📞",
            WizardType.SMS: "📱",
            WizardType.MAIL: "📧",
            WizardType.AGENDA: "📅",
        }
        em = emoji_map.get(wt, "🔧")

        text = ""
        markup = None

        if step == WizardStep.SELECT_CONTACT:
            if wt == WizardType.BELLEN:
                text = f"{em} *Wie wil je bellen?*\nKies een contact of voer een nummer in:"
            elif wt == WizardType.SMS:
                text = f"{em} *Naar wie wil je een SMS sturen?*\nKies een contact of voer een nummer in:"
            elif wt == WizardType.MAIL:
                text = f"{em} *Naar wie wil je mailen?*\nKies een contact of voer een e-mailadres in:"
            markup = await self._build_contact_keyboard(wizard.user_id, wt)

        elif step == WizardStep.ENTER_NUMBER:
            text = f"{em} Typ het telefoonnummer (bijv. +31612345678 of 0612345678):"
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Annuleren", callback_data="wz:cancel")]
            ])

        elif step == WizardStep.ENTER_EMAIL:
            text = f"{em} Typ het e-mailadres:"
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Annuleren", callback_data="wz:cancel")]
            ])

        elif step == WizardStep.ENTER_MESSAGE:
            contact = wizard.contact_name or wizard.phone_number or wizard.email_address or "?"
            if wt == WizardType.BELLEN:
                text = f"{em} Bellen naar *{contact}*\n\nWat wil je dat ik ga zeggen?"
            elif wt == WizardType.SMS:
                text = f"{em} SMS naar *{contact}*\n\nWat wil je sturen?"
            elif wt == WizardType.MAIL:
                text = f"{em} Mail naar *{contact}*\nOnderwerp: _{wizard.subject}_\n\nSchrijf je bericht:"
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Annuleren", callback_data="wz:cancel")]
            ])

        elif step == WizardStep.ENTER_SUBJECT:
            contact = wizard.contact_name or wizard.email_address or "?"
            text = f"{em} Mail naar *{contact}*\n\nWat is het onderwerp?"
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Annuleren", callback_data="wz:cancel")]
            ])

        elif step == WizardStep.ENTER_TITLE:
            text = f"{em} *Nieuwe afspraak*\n\nWat voor afspraak wil je inplannen?"
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Annuleren", callback_data="wz:cancel")]
            ])

        elif step == WizardStep.ENTER_DATETIME:
            text = (
                f"{em} *{wizard.event_title}*\n\n"
                "Wanneer is de afspraak?\n"
                "Kies een optie of typ zelf (bijv. _morgen 10:00_ of _15-02 14:30_):"
            )
            markup = self._build_datetime_keyboard(wt)

        elif step == WizardStep.CONFIRM:
            text = self._build_confirm_summary(wizard)
            markup = self._build_confirm_keyboard(wt)

        # Verstuur of bewerk bericht
        if edit and hasattr(update_or_query, 'edit_message_text'):
            await update_or_query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
        elif hasattr(update_or_query, 'message') and update_or_query.message:
            await update_or_query.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)
        elif hasattr(update_or_query, 'reply_text'):
            await update_or_query.reply_text(text, parse_mode="Markdown", reply_markup=markup)

    def _build_confirm_summary(self, wizard: WizardState) -> str:
        """Bouw samenvatting voor bevestigingsstap"""
        wt = wizard.wizard_type
        contact = wizard.contact_name or wizard.phone_number or wizard.email_address or "?"

        if wt == WizardType.BELLEN:
            phone = wizard.phone_number or ""
            return (
                f"📞 *Klaar om te bellen*\n\n"
                f"👤 Naar: *{contact}*"
                + (f" ({phone})" if phone and phone != contact else "")
                + f"\n💬 Boodschap: _{wizard.message_body}_\n\n"
                "Wil je dat ik nu bel?"
            )
        elif wt == WizardType.SMS:
            phone = wizard.phone_number or ""
            return (
                f"📱 *SMS klaar*\n\n"
                f"👤 Naar: *{contact}*"
                + (f" ({phone})" if phone and phone != contact else "")
                + f"\n💬 Bericht: _{wizard.message_body}_\n\n"
                "Versturen?"
            )
        elif wt == WizardType.MAIL:
            email = wizard.email_address or ""
            return (
                f"📧 *Mail klaar*\n\n"
                f"👤 Aan: *{contact}*"
                + (f" ({email})" if email and email != contact else "")
                + f"\n📝 Onderwerp: _{wizard.subject}_\n"
                "─────────────\n"
                f"{wizard.message_body}\n"
                "─────────────\n\n"
                "Versturen?"
            )
        elif wt == WizardType.AGENDA:
            return (
                f"📅 *Afspraak*\n\n"
                f"📝 {wizard.event_title}\n"
                f"📆 {wizard.event_datetime}\n\n"
                "Toevoegen aan agenda?"
            )
        return "Bevestig je actie?"

    # ── Wizard input processing ────────────────────────────────────────

    async def _process_wizard_input(self, update: Update, wizard: WizardState, text: str):
        """Verwerk tekst-input binnen een actieve wizard"""
        step = wizard.current_step
        user_id = wizard.user_id

        if step == WizardStep.ENTER_NUMBER:
            # Valideer en sla nummer op
            cleaned = text.strip().replace(" ", "").replace("-", "")
            if not cleaned.startswith("+") and not cleaned[0].isdigit():
                await update.message.reply_text("⚠️ Dat lijkt geen geldig nummer. Probeer bijv. +31612345678 of 0612345678")
                return
            wizard.phone_number = cleaned
            self._advance_wizard(user_id)
            await self._render_wizard_step(update, wizard)

        elif step == WizardStep.ENTER_EMAIL:
            if "@" not in text or "." not in text:
                await update.message.reply_text("⚠️ Dat lijkt geen geldig e-mailadres. Probeer opnieuw:")
                return
            wizard.email_address = text.strip()
            self._advance_wizard(user_id)
            await self._render_wizard_step(update, wizard)

        elif step == WizardStep.ENTER_MESSAGE:
            wizard.message_body = text.strip()
            self._advance_wizard(user_id)
            await self._render_wizard_step(update, wizard)

        elif step == WizardStep.ENTER_SUBJECT:
            wizard.subject = text.strip()
            self._advance_wizard(user_id)
            await self._render_wizard_step(update, wizard)

        elif step == WizardStep.ENTER_TITLE:
            wizard.event_title = text.strip()
            self._advance_wizard(user_id)
            await self._render_wizard_step(update, wizard)

        elif step == WizardStep.ENTER_DATETIME:
            wizard.event_datetime = text.strip()
            self._advance_wizard(user_id)
            await self._render_wizard_step(update, wizard)

        elif step == WizardStep.SELECT_CONTACT:
            # Gebruiker typte een naam i.p.v. button te klikken
            await self._handle_wizard_contact_text(update, wizard, text)

        else:
            # Onbekende stap – stuur naar normaal
            self._clear_wizard(user_id)
            await update.message.reply_text("⚠️ Er ging iets mis. Probeer opnieuw via het menu.")

    async def _handle_wizard_contact_text(self, update: Update, wizard: WizardState, text: str):
        """Verwerk getypte contactnaam of nummer in SELECT_CONTACT stap"""
        user_id = wizard.user_id
        cleaned = text.strip()

        # Check of het een telefoonnummer is
        if cleaned.startswith("+") or (cleaned and cleaned[0].isdigit()):
            if wizard.wizard_type == WizardType.MAIL:
                # Bij mail verwachten we een email
                if "@" in cleaned:
                    wizard.email_address = cleaned
                    wizard.contact_name = cleaned
                    self._advance_wizard(user_id)
                    await self._render_wizard_step(update, wizard)
                    return
                else:
                    await update.message.reply_text("⚠️ Voer een e-mailadres in (bijv. jan@email.nl):")
                    return
            else:
                wizard.phone_number = cleaned
                wizard.contact_name = cleaned
                self._advance_wizard(user_id)
                await self._render_wizard_step(update, wizard)
                return

        # Check email
        if "@" in cleaned and wizard.wizard_type == WizardType.MAIL:
            wizard.email_address = cleaned
            wizard.contact_name = cleaned
            self._advance_wizard(user_id)
            await self._render_wizard_step(update, wizard)
            return

        # Probeer contact te zoeken op naam
        if self.memory:
            try:
                contact = await self.memory.get_contact_by_name(user_id, cleaned)
                if contact:
                    wizard.contact_name = contact.get("name", cleaned)
                    wizard.phone_number = contact.get("phone_number")
                    wizard.email_address = contact.get("email")

                    # Check of we het juiste veld hebben
                    if wizard.wizard_type == WizardType.MAIL and not wizard.email_address:
                        await update.message.reply_text(
                            f"⚠️ Contact *{wizard.contact_name}* heeft geen e-mailadres. "
                            "Voer een e-mailadres in of kies een ander contact:",
                            parse_mode="Markdown",
                        )
                        return
                    if wizard.wizard_type != WizardType.MAIL and not wizard.phone_number:
                        await update.message.reply_text(
                            f"⚠️ Contact *{wizard.contact_name}* heeft geen telefoonnummer. "
                            "Voer een nummer in of kies een ander contact:",
                            parse_mode="Markdown",
                        )
                        return

                    self._advance_wizard(user_id)
                    await self._render_wizard_step(update, wizard)
                    return
            except Exception:
                pass

        # Niet gevonden
        await update.message.reply_text(
            f"🔍 Contact '{cleaned}' niet gevonden.\n"
            "Voer een telefoonnummer of e-mailadres in, of kies uit de knoppen hierboven."
        )

    # ── Wizard callback handler ────────────────────────────────────────

    async def handle_wizard_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler voor wizard inline button callbacks"""
        query = update.callback_query
        if not query or not query.data:
            return

        await query.answer()

        user_id = str(update.effective_user.id)
        data = query.data  # bijv. "wz:bellen:ct:abc123"

        # Universeel annuleren
        if data == "wz:cancel":
            self._clear_wizard(user_id)
            await query.edit_message_text("❌ Geannuleerd. Kies een actie uit het menu of typ wat je wilt.")
            return

        parts = data.split(":", 3)
        if len(parts) < 4:
            return

        _, wz_type, step_code, payload = parts

        wizard = self._get_wizard(user_id)
        if not wizard:
            await query.edit_message_text("⚠️ Deze wizard is verlopen. Start opnieuw via het menu.")
            return

        if wizard.wizard_type.value != wz_type:
            await query.edit_message_text("⚠️ Wizard mismatch. Start opnieuw.")
            self._clear_wizard(user_id)
            return

        # Contact geselecteerd
        if step_code == "ct":
            if payload == "manual":
                # Handmatige invoer
                if wizard.wizard_type == WizardType.MAIL:
                    wizard.current_step = WizardStep.ENTER_EMAIL
                else:
                    wizard.current_step = WizardStep.ENTER_NUMBER
                await self._render_wizard_step(query, wizard, edit=True)
            else:
                # Contact ID opgezocht
                await self._handle_contact_selected(query, wizard, payload)

        # Datum/tijd snelkeuze (agenda)
        elif step_code == "dt":
            await self._handle_datetime_selected(query, wizard, payload)

        # Bevestiging
        elif step_code == "ok":
            if payload == "yes":
                await self._execute_wizard(query, wizard)
            else:
                self._clear_wizard(user_id)
                await query.edit_message_text("❌ Geannuleerd.")

    async def _handle_contact_selected(self, query, wizard: WizardState, contact_id: str):
        """Verwerk geselecteerd contact uit inline button"""
        if not self.memory:
            await query.edit_message_text("❌ Contacten niet beschikbaar.")
            self._clear_wizard(wizard.user_id)
            return

        try:
            contacts = await self.memory.get_contacts(wizard.user_id)
            contact = None
            for c in contacts:
                if str(c.get("id", ""))[:20] == contact_id:
                    contact = c
                    break

            if not contact:
                await query.edit_message_text("⚠️ Contact niet gevonden. Start opnieuw.")
                self._clear_wizard(wizard.user_id)
                return

            wizard.contact_name = contact.get("name")
            wizard.phone_number = contact.get("phone_number")
            wizard.email_address = contact.get("email")

            # Valideer dat we het juiste veld hebben
            if wizard.wizard_type == WizardType.MAIL and not wizard.email_address:
                await query.edit_message_text(
                    f"⚠️ *{wizard.contact_name}* heeft geen e-mailadres opgeslagen.\n"
                    "Typ een e-mailadres of kies een ander contact.",
                    parse_mode="Markdown",
                )
                wizard.current_step = WizardStep.ENTER_EMAIL
                return

            if wizard.wizard_type != WizardType.MAIL and not wizard.phone_number:
                await query.edit_message_text(
                    f"⚠️ *{wizard.contact_name}* heeft geen telefoonnummer opgeslagen.\n"
                    "Typ een nummer of kies een ander contact.",
                    parse_mode="Markdown",
                )
                wizard.current_step = WizardStep.ENTER_NUMBER
                return

            self._advance_wizard(wizard.user_id)
            await self._render_wizard_step(query, wizard, edit=True)

        except Exception as e:
            logger.error(f"Error bij contact selectie: {e}")
            await query.edit_message_text("❌ Fout bij ophalen contact.")
            self._clear_wizard(wizard.user_id)

    async def _handle_datetime_selected(self, query, wizard: WizardState, code: str):
        """Verwerk datum/tijd snelkeuze voor agenda wizard"""
        from datetime import datetime, timedelta
        import pytz

        nl_tz = pytz.timezone("Europe/Amsterdam")
        now = datetime.now(nl_tz)

        dt_map = {
            "m09": (now + timedelta(days=1), 9, 0),
            "m14": (now + timedelta(days=1), 14, 0),
            "o10": (now + timedelta(days=2), 10, 0),
            "o15": (now + timedelta(days=2), 15, 0),
        }

        if code == "custom":
            wizard.current_step = WizardStep.ENTER_DATETIME
            # Verwijder knoppen, vraag om tekst input
            await query.edit_message_text(
                f"📅 *{wizard.event_title}*\n\n"
                "Typ de datum en tijd (bijv. _morgen 10:00_ of _15-02 14:30_):",
                parse_mode="Markdown",
            )
            return

        if code in dt_map:
            base, hour, minute = dt_map[code]
            target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
            wizard.event_datetime = target.strftime("%d-%m-%Y om %H:%M")
            self._advance_wizard(wizard.user_id)
            await self._render_wizard_step(query, wizard, edit=True)
        else:
            await query.edit_message_text("⚠️ Ongeldige keuze. Probeer opnieuw.")

    # ── Wizard executie ────────────────────────────────────────────────

    async def _execute_wizard(self, query, wizard: WizardState):
        """Bouw taak-string en voer uit via de orchestrator"""
        user_id = wizard.user_id
        chat_id = wizard.chat_id
        wt = wizard.wizard_type

        # Bouw natuurlijke taak-string
        if wt == WizardType.BELLEN:
            contact = wizard.contact_name or wizard.phone_number
            task = f"Bel {contact}"
            if wizard.message_body:
                task += f" en zeg: {wizard.message_body}"

        elif wt == WizardType.SMS:
            contact = wizard.contact_name or wizard.phone_number
            task = f"Stuur SMS naar {contact} dat {wizard.message_body}"

        elif wt == WizardType.MAIL:
            contact = wizard.contact_name or wizard.email_address
            task = f"Mail {contact} met onderwerp '{wizard.subject}': {wizard.message_body}"

        elif wt == WizardType.AGENDA:
            task = f"Zet {wizard.event_title} in mijn agenda op {wizard.event_datetime}"

        else:
            task = "onbekende actie"

        # Clear wizard vóór executie
        self._clear_wizard(user_id)

        await query.edit_message_text("⏳ Bezig…")

        try:
            result = await process_request(
                user_message=task,
                user_id=user_id,
                confirmed=True,
            )

            response = result.get("response", "Er ging iets mis.")
            await query.edit_message_text(response)

            # Start call polling als het een bel-actie was
            call_id = result.get("call_id")
            if call_id and call_id != "onbekend":
                self._pending_calls[call_id] = {
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "started_at": asyncio.get_event_loop().time(),
                }
                asyncio.create_task(
                    self.poll_call_transcript(call_id, chat_id, user_id)
                )

            # Log in memory
            if self.memory:
                try:
                    conv = await self.memory.get_active_conversation(user_id)
                    if not conv:
                        conv = await self.memory.start_conversation(user_id)
                    await self.memory.add_message(
                        conversation_id=conv["id"],
                        role="user",
                        content=f"[Wizard: {wt.value}] {task}",
                    )
                    await self.memory.add_message(
                        conversation_id=conv["id"],
                        role="assistant",
                        content=response,
                    )
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Wizard executie fout: {e}")
            await query.edit_message_text(f"❌ Er ging iets mis: {str(e)}")

    @staticmethod
    def _looks_like_full_command(text: str) -> bool:
        """Detecteer of tekst een volledig commando is dat de wizard moet bypassen"""
        lower = text.lower().strip()
        patterns = [
            r'^bel\s+\w+',
            r'^sms\s+\w+',
            r'^mail\s+\w+',
            r'^stuur\s+(sms|mail|email)',
            r'^zet\s+.+\s+in\s+(mijn\s+)?agenda',
        ]
        for pattern in patterns:
            if _re_module.match(pattern, lower):
                return True
        return False

    # ── Groeps-SMS/mail ──────────────────────────────────────────────

    async def _handle_group_message(
        self, update: Update, user_id: str,
        category: str, body: str, is_sms: bool = True
    ):
        """
        Verstuur een SMS of mail naar alle contacten in een categorie.
        Toont eerst een overzicht en vraagt om bevestiging.
        """
        if not self.memory:
            await update.message.reply_text("❌ Contacten niet beschikbaar.")
            return

        try:
            contacts = await self.memory.get_contacts(user_id, category=category)
        except Exception:
            contacts = []

        if not contacts:
            await update.message.reply_text(
                f"📇 Geen contacten gevonden in categorie *{category}*.\n"
                "Voeg contacten toe met `/contact add Naam +31612345678 categorie`",
                parse_mode="Markdown",
            )
            return

        msg_type = "SMS" if is_sms else "Mail"
        field = "phone_number" if is_sms else "email"
        valid_contacts = [c for c in contacts if c.get(field)]

        if not valid_contacts:
            missing = "telefoonnummer" if is_sms else "e-mailadres"
            await update.message.reply_text(
                f"⚠️ Geen contacten in *{category}* met een {missing}.",
                parse_mode="Markdown",
            )
            return

        # Toon overzicht
        names = ", ".join(c.get("name", "?") for c in valid_contacts[:10])
        if len(valid_contacts) > 10:
            names += f" en {len(valid_contacts) - 10} meer"

        # Sla op voor bevestiging
        token = uuid4().hex
        self._pending_confirmations[token] = {
            "user_id": user_id,
            "chat_id": str(update.effective_chat.id),
            "message": f"__group_{'sms' if is_sms else 'mail'}__",
            "group_data": {
                "contacts": valid_contacts,
                "body": body,
                "category": category,
                "is_sms": is_sms,
            },
        }

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Ja, verstuur", callback_data=f"confirm:yes:{token}"),
                InlineKeyboardButton("❌ Annuleren", callback_data=f"confirm:no:{token}"),
            ]
        ])

        await update.message.reply_text(
            f"📢 *Groeps-{msg_type}*\n\n"
            f"Categorie: *{category}*\n"
            f"Ontvangers ({len(valid_contacts)}): {names}\n\n"
            f"Bericht: _{body}_\n\n"
            f"Wil je deze {msg_type} naar {len(valid_contacts)} contacten versturen?",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    async def _execute_group_message(self, user_id: str, chat_id: str, group_data: Dict[str, Any]):
        """Voer de daadwerkelijke groeps-SMS/mail uit"""
        contacts = group_data["contacts"]
        body = group_data["body"]
        is_sms = group_data["is_sms"]

        sent = 0
        failed = 0
        errors = []

        if self.application and self.application.bot:
            await self.application.bot.send_message(
                chat_id=int(chat_id),
                text=f"⏳ Bezig met versturen naar {len(contacts)} contacten…"
            )

        for contact in contacts:
            try:
                if is_sms:
                    from ..agents.voice import normalize_e164
                    phone = normalize_e164(contact.get("phone_number", ""))
                    if not phone:
                        failed += 1
                        errors.append(f"{contact.get('name')}: ongeldig nummer")
                        continue
                    result = await send_sms(to_number=phone, body=body)
                    if result.get("success"):
                        sent += 1
                    else:
                        failed += 1
                        errors.append(f"{contact.get('name')}: {result.get('error', '?')[:50]}")
                else:
                    email = contact.get("email")
                    if not email:
                        failed += 1
                        errors.append(f"{contact.get('name')}: geen email")
                        continue
                    import asyncio as _asyncio
                    await _asyncio.to_thread(
                        send_email,
                        to_email=email,
                        subject=f"Bericht van Connect Smart",
                        body_text=body,
                    )
                    sent += 1
            except Exception as e:
                failed += 1
                errors.append(f"{contact.get('name')}: {str(e)[:50]}")

        # Stuur resultaat
        msg_type = "SMS" if is_sms else "Mail"
        message = f"📢 *Groeps-{msg_type} klaar*\n\n"
        message += f"✅ Verstuurd: {sent}\n"
        if failed:
            message += f"❌ Mislukt: {failed}\n"
            for err in errors[:5]:
                message += f"  • {err}\n"

        if self.application and self.application.bot:
            await self.application.bot.send_message(
                chat_id=int(chat_id),
                text=message,
                parse_mode="Markdown",
            )

    # ── Bestaande confirmation helpers ─────────────────────────────────

    def _store_confirmation(self, user_id: str, chat_id: str, message: str) -> str:
        token = uuid4().hex
        self._pending_confirmations[token] = {
            "user_id": user_id,
            "chat_id": chat_id,
            "message": message
        }
        return token

    def _find_pending_confirmation(self, user_id: str):
        for token, data in self._pending_confirmations.items():
            if data.get("user_id") == user_id:
                return token, data
        return None, None

    def _clear_confirmation(self, token: Optional[str]):
        if token:
            self._pending_confirmations.pop(token, None)
    
    async def poll_call_transcript(self, call_id: str, chat_id: int, user_id: str):
        """
        Poll voor call transcript en stuur naar gebruiker wanneer klaar.
        Stuur tussentijdse updates: gaat over, opgenomen / in gesprek.
        
        Args:
            call_id: Vapi call ID
            chat_id: Telegram chat ID om resultaat naar te sturen
            user_id: User ID voor memory
        """
        max_attempts = 60  # Max 5 minuten (60 * 5 sec)
        attempt = 0
        notified_ringing = False
        notified_in_progress = False
        
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
                
                # Tussentijdse status-updates naar Telegram (één keer per status)
                if self.application and self.application.bot:
                    if status == "ringing" and not notified_ringing:
                        notified_ringing = True
                        await self.application.bot.send_message(
                            chat_id=chat_id,
                            text="📞 _Gaat over…_",
                            parse_mode="Markdown"
                        )
                    elif status == "in-progress" and not notified_in_progress:
                        notified_in_progress = True
                        await self.application.bot.send_message(
                            chat_id=chat_id,
                            text="📞 _Opgenomen – in gesprek met de agent._",
                            parse_mode="Markdown"
                        )
                
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

                    # Update call status in memory
                    if self.memory:
                        try:
                            await self.memory.update_call_status(
                                call_id=call_id,
                                status=status,
                                ended_reason=ended_reason or None,
                                duration_seconds=duration,
                                transcript=transcript or None,
                                summary=summary or None
                            )
                        except Exception as e:
                            logger.warning(f"⚠️ Kon call status niet bijwerken: {e}")

                    # ── Proactieve suggesties na gesprek ──
                    if status == "ended" and duration and int(duration) > 5:
                        try:
                            await self._send_post_call_suggestions(
                                chat_id, user_id, call_id,
                                transcript or "", summary or "",
                            )
                        except Exception as e:
                            logger.warning(f"⚠️ Kon suggesties niet sturen: {e}")

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
    
    async def _send_post_call_suggestions(
        self, chat_id: int, user_id: str, call_id: str,
        transcript: str, summary: str
    ):
        """
        Stuur proactieve suggesties na een telefoongesprek.
        Biedt knoppen voor: contact opslaan, herinnering, SMS samenvatting sturen.
        """
        if not self.application or not self.application.bot:
            return

        # Korte samenvatting voor weergave
        short_summary = (summary or transcript)[:100].strip()
        if not short_summary:
            return

        cid = call_id[:16]  # max 64 bytes voor callback_data
        buttons: List[List[InlineKeyboardButton]] = []

        # Contact opslaan suggestie
        buttons.append([
            InlineKeyboardButton(
                "📇 Contact opslaan",
                callback_data=f"ps:save:{cid}",
            )
        ])

        # Herinnering instellen
        buttons.append([
            InlineKeyboardButton(
                "⏰ Herinnering zetten",
                callback_data=f"ps:remind:{cid}",
            )
        ])

        # SMS samenvatting sturen
        buttons.append([
            InlineKeyboardButton(
                "📱 SMS samenvatting sturen",
                callback_data=f"ps:sms:{cid}",
            )
        ])

        # Sla call info op voor callbacks
        self._post_call_data = getattr(self, "_post_call_data", {})
        self._post_call_data[cid] = {
            "user_id": user_id,
            "chat_id": chat_id,
            "call_id": call_id,
            "transcript": transcript,
            "summary": summary,
        }

        await self.application.bot.send_message(
            chat_id=chat_id,
            text=f"💡 *Wat wil je nu doen?*\n_{short_summary}_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def handle_post_call_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler voor proactieve suggestie knoppen na een telefoongesprek"""
        query = update.callback_query
        if not query or not query.data:
            return

        await query.answer()

        user_id = str(update.effective_user.id)
        data = query.data  # "ps:save:abc123", "ps:remind:abc123", "ps:sms:abc123"
        parts = data.split(":", 2)
        if len(parts) < 3:
            return

        _, action, cid = parts

        post_data = getattr(self, "_post_call_data", {}).get(cid)
        if not post_data or post_data.get("user_id") != user_id:
            await query.edit_message_text("⚠️ Deze suggestie is verlopen.")
            return

        chat_id = post_data["chat_id"]
        call_id = post_data["call_id"]
        transcript = post_data.get("transcript", "")
        summary = post_data.get("summary", "")

        if action == "save":
            # Vraag om contactgegevens
            await query.edit_message_text(
                "📇 *Contact opslaan*\n\n"
                "Typ de gegevens in dit formaat:\n"
                "`/contact add Naam +31612345678 categorie`\n\n"
                "Categorieën: restaurant, bedrijf, persoonlijk, overig",
                parse_mode="Markdown",
            )

        elif action == "remind":
            # Vraag om herinnering details
            await query.edit_message_text(
                "⏰ *Herinnering instellen*\n\n"
                "Typ je herinnering, bijvoorbeeld:\n"
                "`/herinner morgen 10:00 Nabellen over gesprek`\n"
                "`/herinner over 30 minuten Follow-up doen`",
                parse_mode="Markdown",
            )

        elif action == "sms":
            # Start SMS wizard met samenvatting als bericht
            short = (summary or transcript[:200]).strip()
            self._clear_wizard(user_id)
            wizard = self._start_wizard(user_id, chat_id, WizardType.SMS)
            wizard.message_body = f"Samenvatting gesprek: {short}"
            await query.edit_message_text("📱 *SMS met samenvatting*\nKies de ontvanger:")
            await self._render_wizard_step(query, wizard)

    async def handle_confirmation_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler voor inline bevestigingsknoppen"""
        query = update.callback_query
        if not query or not query.data:
            return

        await query.answer()

        if not query.data.startswith("confirm:"):
            return

        parts = query.data.split(":", 2)
        if len(parts) != 3:
            return

        action, token = parts[1], parts[2]
        pending = self._pending_confirmations.get(token)
        if not pending:
            await query.edit_message_text("Deze bevestiging is verlopen. Stuur je verzoek opnieuw.")
            return

        user_id = str(update.effective_user.id)
        if pending.get("user_id") != user_id:
            await query.edit_message_text("Deze bevestiging hoort bij een andere gebruiker.")
            return

        # Verwijder pending confirmation
        self._clear_confirmation(token)

        if action == "no":
            await query.edit_message_text("❌ Geannuleerd. Zeg maar als ik iets anders kan doen.")
            return

        if action == "yes":
            # Check of het een groepsbericht is
            pending_msg = pending.get("message", "")
            group_data = pending.get("group_data")
            if pending_msg.startswith("__group_") and group_data:
                await query.edit_message_text("⏳ Groepsbericht wordt verstuurd…")
                chat_id = pending.get("chat_id") or str(query.message.chat_id if query.message else "")
                asyncio.create_task(
                    self._execute_group_message(user_id, chat_id, group_data)
                )
                return

            result = await process_request(
                user_message=pending_msg,
                user_id=user_id,
                confirmed=True
            )
            response = result.get("response", "Er ging iets mis.")
            await query.edit_message_text(response)

            call_id = result.get("call_id")
            chat_id = pending.get("chat_id") or (query.message.chat_id if query.message else None)
            if call_id and chat_id:
                self._pending_calls[call_id] = {
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "started_at": asyncio.get_event_loop().time()
                }
                asyncio.create_task(
                    self.poll_call_transcript(call_id, chat_id, user_id)
                )
            return

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler voor normale tekstberichten"""
        user = update.effective_user
        message_text = update.message.text
        user_id = str(user.id)
        chat_id = update.effective_chat.id

        lower = message_text.strip().lower()

        # ── Menu button detectie ────────────────────────────────────
        menu_mapping = {
            "📞 bellen": WizardType.BELLEN,
            "📱 sms": WizardType.SMS,
            "📧 mail": WizardType.MAIL,
            "📅 agenda": WizardType.AGENDA,
        }

        if lower in menu_mapping:
            self._clear_wizard(user_id)
            self._clear_confirmation(self._find_pending_confirmation(user_id)[0])
            wizard = self._start_wizard(user_id, chat_id, menu_mapping[lower])
            await self._render_wizard_step(update, wizard)
            return

        if lower == "📇 contacten":
            await self.contact_command(update, context)
            return

        if lower == "❓ help":
            await self.help_command(update, context)
            return

        if lower == "🕐 vrije tijd":
            await self.vrije_tijd_command(update, context)
            return

        if lower == "📊 overzicht":
            await self.overzicht_command(update, context)
            return

        # ── Groeps-SMS/mail detectie ─────────────────────────────
        group_sms_match = _re_module.match(
            r'^(?:sms|stuur\s+sms)\s+(?:iedereen|allemaal|groep)\s+(?:uit|van|in)\s+(\w+)\s+(?:dat\s+)?(.+)',
            lower,
        )
        group_mail_match = _re_module.match(
            r'^(?:mail|stuur\s+mail)\s+(?:iedereen|allemaal|groep)\s+(?:uit|van|in)\s+(\w+)\s+(?:dat\s+)?(.+)',
            lower,
        )
        if group_sms_match or group_mail_match:
            match = group_sms_match or group_mail_match
            is_sms = group_sms_match is not None
            category = match.group(1)
            body = match.group(2).strip()
            await self._handle_group_message(update, user_id, category, body, is_sms=is_sms)
            return

        # ── Actieve wizard check ────────────────────────────────────
        wizard = self._get_wizard(user_id)
        if wizard:
            cancel_words = {"annuleer", "stop", "cancel", "terug"}
            if lower in cancel_words:
                self._clear_wizard(user_id)
                await update.message.reply_text(
                    "❌ Geannuleerd. Kies een actie uit het menu of typ wat je wilt.",
                    reply_markup=self._get_main_menu_keyboard(),
                )
                return

            # Escape hatch: volledig commando bypass wizard
            if self._looks_like_full_command(message_text):
                self._clear_wizard(user_id)
                # Val door naar normale verwerking hieronder
            else:
                await self._process_wizard_input(update, wizard, message_text)
                return

        # ── Bestaande confirmation flow ─────────────────────────────
        yes_words = {"ja", "yes", "oke", "ok", "doe maar", "ga door", "bevestig"}
        no_words = {"nee", "no", "stop", "annuleer", "cancel"}
        token, pending = self._find_pending_confirmation(user_id)

        if pending:
            if lower in yes_words:
                self._clear_confirmation(token)
                result = await process_request(
                    user_message=pending.get("message", ""),
                    user_id=user_id,
                    confirmed=True
                )
                response = result.get("response", "Er ging iets mis.")
                await update.message.reply_text(response)
                call_id = result.get("call_id")
                if call_id:
                    self._pending_calls[call_id] = {
                        "chat_id": chat_id,
                        "user_id": user_id,
                        "started_at": asyncio.get_event_loop().time()
                    }
                    asyncio.create_task(
                        self.poll_call_transcript(call_id, chat_id, user_id)
                    )
                return
            if lower in no_words:
                self._clear_confirmation(token)
                await update.message.reply_text("❌ Geannuleerd. Zeg maar als ik iets anders kan doen.")
                return
            # Nieuwe taak: vervang oude bevestiging
            self._clear_confirmation(token)
        
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
            voice_result = result.get("voice_result") or {}
            voice_result = result.get("voice_result") or {}
            
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
            
            # Stuur response terug (met bevestigingsknoppen indien nodig)
            if result.get("needs_confirmation"):
                pending_message = result.get("pending_action") or message_text
                token = self._store_confirmation(user_id, str(chat_id), pending_message)
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Ja, doen", callback_data=f"confirm:yes:{token}"),
                        InlineKeyboardButton("❌ Nee, stop", callback_data=f"confirm:no:{token}"),
                    ]
                ])
                await update.message.reply_text(response, reply_markup=keyboard)
                return

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

                # Log call in memory
                if self.memory:
                    try:
                        await self.memory.log_call(
                            telegram_id=user_id,
                            call_id=call_id,
                            phone_number=voice_result.get("phone_number") or "unknown",
                            call_type="general",
                            metadata={"source": "telegram"}
                        )
                    except Exception as e:
                        logger.warning(f"⚠️ Kon call niet loggen: {e}")
            
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
            
            if result.get("needs_confirmation"):
                pending_message = result.get("pending_action") or message_text
                token = self._store_confirmation(user_id, str(chat_id), pending_message)
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Ja, doen", callback_data=f"confirm:yes:{token}"),
                        InlineKeyboardButton("❌ Nee, stop", callback_data=f"confirm:no:{token}"),
                    ]
                ])
                await update.message.reply_text(response, reply_markup=keyboard)
                return

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

                if self.memory:
                    try:
                        await self.memory.log_call(
                            telegram_id=user_id,
                            call_id=call_id,
                            phone_number=voice_result.get("phone_number") or "unknown",
                            call_type="general",
                            metadata={"source": "telegram", "type": "voice"}
                        )
                    except Exception as e:
                        logger.warning(f"⚠️ Kon call niet loggen: {e}")
            
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
        self.application.add_handler(CommandHandler("menu", self.menu_command))
        self.application.add_handler(CommandHandler("overzicht", self.overzicht_command))
        self.application.add_handler(CommandHandler("vrijetijd", self.vrije_tijd_command))

        # Post-call suggestie callbacks
        self.application.add_handler(CallbackQueryHandler(self.handle_post_call_callback, pattern=r"^ps:"))

        # Wizard callbacks (vóór confirmation — meer specifiek patroon)
        self.application.add_handler(CallbackQueryHandler(self.handle_wizard_callback, pattern=r"^wz:"))

        # Confirmation buttons
        self.application.add_handler(CallbackQueryHandler(self.handle_confirmation_callback, pattern=r"^confirm:"))
        
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
