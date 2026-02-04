"""
Reminder Scheduler - Verzendt herinneringen naar Telegram gebruikers.

Draait als achtergrondtaak en checkt elke minuut voor due reminders.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Callable, Awaitable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from ..config import get_settings
from ..memory.supabase import get_memory_system

logger = logging.getLogger(__name__)


class ReminderScheduler:
    """Scheduler voor het versturen van herinneringen"""
    
    def __init__(self):
        self.settings = get_settings()
        self.scheduler: Optional[AsyncIOScheduler] = None
        self.memory = None
        self._send_message_callback: Optional[Callable[[str, str], Awaitable[None]]] = None
        
        # Initialize memory if configured
        if self.settings.supabase_url and self.settings.supabase_anon_key:
            try:
                self.memory = get_memory_system()
            except Exception as e:
                logger.warning(f"⚠️ Memory niet beschikbaar voor scheduler: {e}")
    
    def set_message_callback(self, callback: Callable[[str, str], Awaitable[None]]):
        """
        Stel de callback in voor het versturen van berichten.
        
        Args:
            callback: Async functie die (chat_id, message) accepteert
        """
        self._send_message_callback = callback
    
    async def check_and_send_reminders(self):
        """Check voor due reminders en verstuur ze"""
        if not self.memory:
            return
        
        if not self._send_message_callback:
            logger.warning("⚠️ Geen message callback ingesteld")
            return
        
        try:
            # Haal due reminders op
            due_reminders = await self.memory.get_due_reminders()
            
            for reminder in due_reminders:
                chat_id = reminder.get("telegram_chat_id")
                message = reminder.get("message")
                reminder_id = reminder.get("id")
                user_info = reminder.get("agent_users", {})
                user_name = user_info.get("first_name", "")
                
                if not chat_id or not message:
                    continue
                
                # Bouw het bericht
                greeting = f"Hoi {user_name}! " if user_name else ""
                full_message = f"⏰ *Herinnering*\n\n{greeting}{message}"
                
                try:
                    # Verstuur via callback
                    await self._send_message_callback(chat_id, full_message)
                    
                    # Markeer als verzonden
                    await self.memory.mark_reminder_sent(reminder_id)
                    
                    logger.info(f"✅ Herinnering verzonden naar chat {chat_id}")
                    
                    # Check voor herhaling
                    repeat = reminder.get("repeat_interval")
                    if repeat:
                        await self._schedule_repeat(reminder, repeat)
                        
                except Exception as e:
                    logger.error(f"❌ Kon herinnering niet verzenden: {e}")
                    
        except Exception as e:
            logger.error(f"❌ Error bij check reminders: {e}")
    
    async def _schedule_repeat(self, reminder: dict, interval: str):
        """Plan een herhalende herinnering opnieuw in"""
        if not self.memory:
            return
        
        # Bereken nieuwe tijd
        current_time = datetime.fromisoformat(reminder["remind_at"].replace("Z", "+00:00"))
        
        if interval == "daily":
            new_time = current_time + timedelta(days=1)
        elif interval == "weekly":
            new_time = current_time + timedelta(weeks=1)
        elif interval == "monthly":
            new_time = current_time + timedelta(days=30)
        else:
            return  # Onbekend interval
        
        # Maak nieuwe herinnering
        user_info = reminder.get("agent_users", {})
        telegram_id = user_info.get("telegram_id")
        
        if telegram_id:
            await self.memory.add_reminder(
                telegram_id=telegram_id,
                message=reminder["message"],
                remind_at=new_time,
                telegram_chat_id=reminder["telegram_chat_id"],
                repeat_interval=interval
            )
            logger.info(f"📅 Herhalende herinnering ingepland voor {new_time}")
    
    def start(self):
        """Start de scheduler"""
        if self.scheduler:
            return
        
        self.scheduler = AsyncIOScheduler()
        
        # Check elke minuut voor due reminders
        self.scheduler.add_job(
            self.check_and_send_reminders,
            trigger=IntervalTrigger(minutes=1),
            id="reminder_check",
            name="Check en verstuur herinneringen",
            replace_existing=True
        )
        
        self.scheduler.start()
        logger.info("⏰ Reminder scheduler gestart")
    
    def stop(self):
        """Stop de scheduler"""
        if self.scheduler:
            self.scheduler.shutdown()
            self.scheduler = None
            logger.info("⏰ Reminder scheduler gestopt")


# Singleton instance
_scheduler: Optional[ReminderScheduler] = None


def get_reminder_scheduler() -> ReminderScheduler:
    """Get or create the reminder scheduler instance"""
    global _scheduler
    if _scheduler is None:
        _scheduler = ReminderScheduler()
    return _scheduler
