"""
Telegram Channel - Integratie met Telegram Bot API.

Ontvangt berichten via polling en stuurt responses terug.
Veel simpeler dan WhatsApp - geen webhook of verificatie nodig!
"""
import asyncio
import logging
from typing import Optional
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
        
        # Initialize memory if Supabase is configured
        if self.settings.supabase_url and self.settings.supabase_anon_key:
            try:
                self.memory = get_memory_system()
                logger.info("💾 Memory system geactiveerd (Supabase)")
            except Exception as e:
                logger.warning(f"⚠️ Memory system niet beschikbaar: {e}")
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler voor /start command"""
        welcome_message = """
🤖 *Welkom bij AI Agent Belt!*

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

🍽️ "Reserveer een tafel voor 4 personen bij Ciel Bleu op vrijdag 19:30"

🔎 "Zoek restaurants met een Michelin ster in Rotterdam"

📅 "Wat zijn de openingstijden van Restaurant De Librije?"

🎫 "Zoek tickets voor een jazz concert in Utrecht"

*Commands:*
/start - Welkomstbericht
/help - Deze hulp
/status - Check systeem status

Typ gewoon wat je wilt en ik ga aan de slag! 🚀
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

🤖 Bot: @ai_agent_belt_bot
📡 Versie: 1.1.0
        """
        await update.message.reply_text(
            status_message,
            parse_mode="Markdown"
        )
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler voor normale tekstberichten"""
        user = update.effective_user
        message_text = update.message.text
        user_id = str(user.id)
        
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
            response = await process_request(
                user_message=message_text,
                user_id=user_id
            )
            
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
            
        except Exception as e:
            logger.error(f"❌ Error bij verwerken bericht: {e}")
            await update.message.reply_text(
                "Sorry, er ging iets mis bij het verwerken van je verzoek. "
                "Probeer het opnieuw of typ /help voor hulp."
            )
    
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
        
        # Message handler voor alle tekst berichten
        self.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message)
        )
        
        # Error handler
        self.application.add_error_handler(self.error_handler)
        
        return self.application
    
    async def run_polling(self):
        """Start de bot met polling"""
        app = self.build_application()
        
        logger.info("🤖 Telegram bot gestart!")
        logger.info("📱 Bot: @ai_agent_belt_bot")
        
        # Initialize en start polling
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        
        # Houd de bot draaiende
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
    
    def run(self):
        """Start de bot (blocking)"""
        app = self.build_application()
        logger.info("🤖 Telegram bot gestart!")
        logger.info("📱 Bot: @ai_agent_belt_bot")
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
