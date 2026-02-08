"""
Supabase Memory System - Lange-termijn geheugen voor Connect Smart.

Slaat op:
- Gebruikersprofielen en voorkeuren
- Conversatie geschiedenis
- Belangrijke feiten en patronen
"""
from typing import Optional, List, Dict, Any
from datetime import datetime
import json
from supabase import create_client, Client

from ..config import get_settings


class MemorySystem:
    """Geheugen systeem met Supabase als backend"""
    
    def __init__(self):
        self.settings = get_settings()
        self._client: Optional[Client] = None
    
    @property
    def client(self) -> Client:
        """Lazy initialization van Supabase client"""
        if self._client is None:
            if not self.settings.supabase_url or not self.settings.supabase_anon_key:
                raise ValueError("Supabase niet geconfigureerd")
            self._client = create_client(
                self.settings.supabase_url,
                self.settings.supabase_anon_key
            )
        return self._client
    
    # === USER MANAGEMENT ===
    
    async def get_or_create_user(
        self,
        telegram_id: str,
        telegram_username: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Haal gebruiker op of maak nieuwe aan.
        
        Args:
            telegram_id: Telegram user ID
            telegram_username: Telegram username
            first_name: Voornaam
            last_name: Achternaam
            
        Returns:
            User record
        """
        # Probeer bestaande user te vinden
        result = self.client.table("agent_users").select("*").eq(
            "telegram_id", telegram_id
        ).execute()
        
        if result.data:
            user = result.data[0]
            # Update username/naam als die veranderd is
            updates = {}
            if telegram_username and user.get("telegram_username") != telegram_username:
                updates["telegram_username"] = telegram_username
            if first_name and user.get("first_name") != first_name:
                updates["first_name"] = first_name
            if last_name and user.get("last_name") != last_name:
                updates["last_name"] = last_name
            
            if updates:
                self.client.table("agent_users").update(updates).eq(
                    "id", user["id"]
                ).execute()
                user.update(updates)
            
            return user
        
        # Maak nieuwe user
        new_user = {
            "telegram_id": telegram_id,
            "telegram_username": telegram_username,
            "first_name": first_name,
            "last_name": last_name,
            "preferences": {}
        }
        
        result = self.client.table("agent_users").insert(new_user).execute()
        return result.data[0]
    
    async def update_user_preferences(
        self,
        telegram_id: str,
        preferences: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update gebruikersvoorkeuren"""
        # Haal huidige preferences op
        result = self.client.table("agent_users").select("preferences").eq(
            "telegram_id", telegram_id
        ).execute()
        
        if not result.data:
            raise ValueError(f"User {telegram_id} niet gevonden")
        
        current_prefs = result.data[0].get("preferences", {})
        current_prefs.update(preferences)
        
        # Update
        self.client.table("agent_users").update({
            "preferences": current_prefs
        }).eq("telegram_id", telegram_id).execute()
        
        return current_prefs
    
    # === CONVERSATION MANAGEMENT ===
    
    async def start_conversation(self, telegram_id: str) -> Dict[str, Any]:
        """Start een nieuwe conversatie"""
        user = await self.get_or_create_user(telegram_id)
        
        result = self.client.table("agent_conversations").insert({
            "user_id": user["id"],
            "metadata": {}
        }).execute()
        
        return result.data[0]
    
    async def get_active_conversation(self, telegram_id: str) -> Optional[Dict[str, Any]]:
        """Haal actieve (niet-afgesloten) conversatie op"""
        user = await self.get_or_create_user(telegram_id)
        
        result = self.client.table("agent_conversations").select("*").eq(
            "user_id", user["id"]
        ).is_("ended_at", "null").order(
            "started_at", desc=True
        ).limit(1).execute()
        
        return result.data[0] if result.data else None
    
    async def end_conversation(
        self,
        conversation_id: str,
        summary: Optional[str] = None
    ):
        """Beëindig een conversatie"""
        self.client.table("agent_conversations").update({
            "ended_at": datetime.utcnow().isoformat(),
            "summary": summary
        }).eq("id", conversation_id).execute()
    
    # === MESSAGE MANAGEMENT ===
    
    async def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Voeg een bericht toe aan de conversatie.
        
        Args:
            conversation_id: ID van de conversatie
            role: 'user', 'assistant', of 'system'
            content: Inhoud van het bericht
            metadata: Extra metadata
        """
        result = self.client.table("agent_messages").insert({
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
            "metadata": metadata or {}
        }).execute()
        
        return result.data[0]
    
    async def get_conversation_history(
        self,
        conversation_id: str,
        limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Haal conversatie geschiedenis op"""
        result = self.client.table("agent_messages").select("*").eq(
            "conversation_id", conversation_id
        ).order("created_at", desc=False).limit(limit).execute()
        
        return result.data
    
    async def get_recent_messages(
        self,
        telegram_id: str,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Haal recente berichten op voor een gebruiker"""
        user = await self.get_or_create_user(telegram_id)
        
        # Haal recente conversaties
        convs = self.client.table("agent_conversations").select("id").eq(
            "user_id", user["id"]
        ).order("started_at", desc=True).limit(3).execute()
        
        if not convs.data:
            return []
        
        conv_ids = [c["id"] for c in convs.data]
        
        # Haal berichten
        result = self.client.table("agent_messages").select("*").in_(
            "conversation_id", conv_ids
        ).order("created_at", desc=True).limit(limit).execute()
        
        return list(reversed(result.data))
    
    # === MEMORY MANAGEMENT ===
    
    async def add_memory(
        self,
        telegram_id: str,
        memory_type: str,
        content: str,
        importance: int = 5
    ) -> Dict[str, Any]:
        """
        Voeg een herinnering toe.
        
        Args:
            telegram_id: Telegram user ID
            memory_type: 'preference', 'fact', 'pattern', of 'reminder'
            content: De herinnering
            importance: Belangrijk 1-10
        """
        user = await self.get_or_create_user(telegram_id)
        
        result = self.client.table("agent_memory").insert({
            "user_id": user["id"],
            "memory_type": memory_type,
            "content": content,
            "importance": min(max(importance, 1), 10)
        }).execute()
        
        return result.data[0]
    
    async def get_memories(
        self,
        telegram_id: str,
        memory_type: Optional[str] = None,
        limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Haal herinneringen op voor een gebruiker"""
        user = await self.get_or_create_user(telegram_id)
        
        query = self.client.table("agent_memory").select("*").eq(
            "user_id", user["id"]
        )
        
        if memory_type:
            query = query.eq("memory_type", memory_type)
        
        result = query.order("importance", desc=True).order(
            "last_accessed_at", desc=True
        ).limit(limit).execute()
        
        # Update access count
        if result.data:
            memory_ids = [m["id"] for m in result.data]
            for mid in memory_ids:
                self.client.table("agent_memory").update({
                    "last_accessed_at": datetime.utcnow().isoformat(),
                    "access_count": self.client.table("agent_memory").select(
                        "access_count"
                    ).eq("id", mid).execute().data[0]["access_count"] + 1
                }).eq("id", mid).execute()
        
        return result.data
    
    async def search_memories(
        self,
        telegram_id: str,
        query: str
    ) -> List[Dict[str, Any]]:
        """Zoek in herinneringen (simpele text search)"""
        user = await self.get_or_create_user(telegram_id)
        
        result = self.client.table("agent_memory").select("*").eq(
            "user_id", user["id"]
        ).ilike("content", f"%{query}%").limit(10).execute()
        
        return result.data
    
    # === CONTACT MANAGEMENT ===
    
    async def add_contact(
        self,
        telegram_id: str,
        name: str,
        phone_number: Optional[str] = None,
        email: Optional[str] = None,
        category: str = "overig",
        notes: Optional[str] = None,
        metadata: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Voeg een contact toe.
        
        Args:
            telegram_id: Telegram user ID
            name: Naam van het contact
            phone_number: Telefoonnummer
            email: E-mailadres
            category: Categorie (restaurant, bedrijf, persoonlijk, overig)
            notes: Notities
            metadata: Extra metadata
        """
        user = await self.get_or_create_user(telegram_id)
        
        contact_data = {
            "user_id": user["id"],
            "name": name,
            "phone_number": phone_number,
            "email": email,
            "category": category,
            "notes": notes,
            "metadata": metadata or {}
        }
        
        result = self.client.table("agent_contacts").insert(contact_data).execute()
        return result.data[0]
    
    async def get_contacts(
        self,
        telegram_id: str,
        category: Optional[str] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Haal contacten op voor een gebruiker"""
        user = await self.get_or_create_user(telegram_id)
        
        query = self.client.table("agent_contacts").select("*").eq(
            "user_id", user["id"]
        )
        
        if category:
            query = query.eq("category", category)
        
        result = query.order("name").limit(limit).execute()
        return result.data
    
    async def search_contacts(
        self,
        telegram_id: str,
        search_term: str
    ) -> List[Dict[str, Any]]:
        """Zoek contacten op naam"""
        user = await self.get_or_create_user(telegram_id)
        
        result = self.client.table("agent_contacts").select("*").eq(
            "user_id", user["id"]
        ).ilike("name", f"%{search_term}%").limit(10).execute()
        
        return result.data
    
    async def get_contact_by_name(
        self,
        telegram_id: str,
        name: str
    ) -> Optional[Dict[str, Any]]:
        """Vind een contact op exacte of gedeeltelijke naam"""
        user = await self.get_or_create_user(telegram_id)
        
        # Probeer eerst exact
        result = self.client.table("agent_contacts").select("*").eq(
            "user_id", user["id"]
        ).ilike("name", name).limit(1).execute()
        
        if result.data:
            return result.data[0]
        
        # Probeer gedeeltelijke match
        result = self.client.table("agent_contacts").select("*").eq(
            "user_id", user["id"]
        ).ilike("name", f"%{name}%").limit(1).execute()
        
        return result.data[0] if result.data else None
    
    async def update_contact(
        self,
        telegram_id: str,
        contact_id: str,
        updates: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update een contact"""
        user = await self.get_or_create_user(telegram_id)
        
        # Zorg dat alleen eigen contacten worden bijgewerkt
        updates["updated_at"] = datetime.utcnow().isoformat()
        
        self.client.table("agent_contacts").update(updates).eq(
            "id", contact_id
        ).eq("user_id", user["id"]).execute()
        
        result = self.client.table("agent_contacts").select("*").eq(
            "id", contact_id
        ).execute()
        
        return result.data[0] if result.data else {}
    
    async def delete_contact(
        self,
        telegram_id: str,
        contact_id: str
    ) -> bool:
        """Verwijder een contact"""
        user = await self.get_or_create_user(telegram_id)
        
        self.client.table("agent_contacts").delete().eq(
            "id", contact_id
        ).eq("user_id", user["id"]).execute()
        
        return True
    
    # === REMINDER MANAGEMENT ===
    
    async def add_reminder(
        self,
        telegram_id: str,
        message: str,
        remind_at: datetime,
        telegram_chat_id: str,
        repeat_interval: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Voeg een herinnering toe.
        
        Args:
            telegram_id: Telegram user ID
            message: De herinnering tekst
            remind_at: Wanneer de herinnering moet afgaan
            telegram_chat_id: Chat ID voor het sturen van de herinnering
            repeat_interval: Herhaling (daily, weekly, monthly, of None)
        """
        user = await self.get_or_create_user(telegram_id)
        
        reminder_data = {
            "user_id": user["id"],
            "message": message,
            "remind_at": remind_at.isoformat(),
            "telegram_chat_id": telegram_chat_id,
            "repeat_interval": repeat_interval,
            "is_sent": False
        }
        
        result = self.client.table("agent_reminders").insert(reminder_data).execute()
        return result.data[0]
    
    async def get_due_reminders(self) -> List[Dict[str, Any]]:
        """Haal alle herinneringen op die nu verstuurd moeten worden"""
        now = datetime.utcnow().isoformat()
        
        result = self.client.table("agent_reminders").select(
            "*, agent_users!inner(telegram_id, first_name)"
        ).lte("remind_at", now).eq("is_sent", False).execute()
        
        return result.data
    
    async def mark_reminder_sent(self, reminder_id: str):
        """Markeer een herinnering als verstuurd"""
        self.client.table("agent_reminders").update({
            "is_sent": True,
            "sent_at": datetime.utcnow().isoformat()
        }).eq("id", reminder_id).execute()
    
    async def get_user_reminders(
        self,
        telegram_id: str,
        include_sent: bool = False,
        limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Haal herinneringen op voor een gebruiker"""
        user = await self.get_or_create_user(telegram_id)
        
        query = self.client.table("agent_reminders").select("*").eq(
            "user_id", user["id"]
        )
        
        if not include_sent:
            query = query.eq("is_sent", False)
        
        result = query.order("remind_at").limit(limit).execute()
        return result.data
    
    async def delete_reminder(
        self,
        telegram_id: str,
        reminder_id: str
    ) -> bool:
        """Verwijder een herinnering"""
        user = await self.get_or_create_user(telegram_id)
        
        self.client.table("agent_reminders").delete().eq(
            "id", reminder_id
        ).eq("user_id", user["id"]).execute()
        
        return True
    
    # === CALL LOGGING ===

    async def log_call(
        self,
        telegram_id: Optional[str],
        call_id: str,
        phone_number: str,
        call_type: str,
        metadata: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Log een uitgaande call.

        Args:
            telegram_id: Telegram user ID (kan None zijn voor API calls)
            call_id: Vapi call ID
            phone_number: Gebeld nummer
            call_type: Type call (restaurant_reservation, general, etc.)
            metadata: Extra data (restaurant naam, datum, etc.)
        """
        user_id = None
        if telegram_id:
            user = await self.get_or_create_user(telegram_id)
            user_id = user["id"]

        call_data = {
            "user_id": user_id,
            "call_id": call_id,
            "phone_number": phone_number,
            "call_type": call_type,
            "status": "initiated",
            "metadata": metadata or {}
        }

        result = self.client.table("agent_calls").insert(call_data).execute()
        return result.data[0] if result.data else {}

    async def update_call_status(
        self,
        call_id: str,
        status: str,
        ended_reason: Optional[str] = None,
        duration_seconds: Optional[int] = None,
        transcript: Optional[str] = None,
        summary: Optional[str] = None,
        success: Optional[bool] = None
    ) -> Dict[str, Any]:
        """
        Update de status van een call.

        Args:
            call_id: Vapi call ID
            status: Nieuwe status (ringing, in-progress, ended)
            ended_reason: Reden van beëindiging
            duration_seconds: Duur van het gesprek
            transcript: Volledige transcript
            summary: AI-gegenereerde samenvatting
            success: Of de call succesvol was (bijv. reservering gelukt)
        """
        updates = {
            "status": status,
            "updated_at": datetime.utcnow().isoformat()
        }

        if ended_reason:
            updates["ended_reason"] = ended_reason
        if duration_seconds is not None:
            updates["duration_seconds"] = duration_seconds
        if transcript:
            updates["transcript"] = transcript
        if summary:
            updates["summary"] = summary
        if success is not None:
            updates["success"] = success
        if status == "ended":
            updates["ended_at"] = datetime.utcnow().isoformat()

        self.client.table("agent_calls").update(updates).eq(
            "call_id", call_id
        ).execute()

        result = self.client.table("agent_calls").select("*").eq(
            "call_id", call_id
        ).execute()

        return result.data[0] if result.data else {}

    async def get_call_history(
        self,
        telegram_id: str,
        limit: int = 20,
        call_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Haal call geschiedenis op voor een gebruiker"""
        user = await self.get_or_create_user(telegram_id)

        query = self.client.table("agent_calls").select("*").eq(
            "user_id", user["id"]
        )

        if call_type:
            query = query.eq("call_type", call_type)

        result = query.order("created_at", desc=True).limit(limit).execute()
        return result.data

    async def get_call_stats(
        self,
        telegram_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Haal call statistieken op.

        Args:
            telegram_id: Filter op gebruiker (None = alle gebruikers)

        Returns:
            Dict met statistieken (totaal, succes rate, gem. duur, etc.)
        """
        query = self.client.table("agent_calls").select("*")

        if telegram_id:
            user = await self.get_or_create_user(telegram_id)
            query = query.eq("user_id", user["id"])

        result = query.execute()
        calls = result.data

        if not calls:
            return {
                "total_calls": 0,
                "success_rate": 0,
                "avg_duration": 0,
                "calls_by_type": {}
            }

        total = len(calls)
        successful = sum(1 for c in calls if c.get("success"))
        durations = [c.get("duration_seconds", 0) for c in calls if c.get("duration_seconds")]

        # Group by type
        by_type = {}
        for call in calls:
            ct = call.get("call_type", "unknown")
            if ct not in by_type:
                by_type[ct] = {"total": 0, "successful": 0}
            by_type[ct]["total"] += 1
            if call.get("success"):
                by_type[ct]["successful"] += 1

        return {
            "total_calls": total,
            "successful_calls": successful,
            "success_rate": round(successful / total * 100, 1) if total > 0 else 0,
            "avg_duration_seconds": round(sum(durations) / len(durations), 1) if durations else 0,
            "calls_by_type": by_type
        }

    # === WEEKLY ACTIVITY ===

    async def get_weekly_activity(
        self,
        telegram_id: str,
    ) -> Dict[str, Any]:
        """
        Haal activiteiten van de afgelopen 7 dagen op voor weekoverzicht.

        Returns:
            Dict met calls, sms_messages, emails, reminders
        """
        from datetime import datetime, timedelta

        user = await self.get_or_create_user(telegram_id)
        user_db_id = user["id"]
        week_ago = (datetime.utcnow() - timedelta(days=7)).isoformat()

        # Calls
        calls = []
        try:
            result = self.client.table("agent_calls").select("*").eq(
                "user_id", user_db_id
            ).gte("created_at", week_ago).order("created_at", desc=True).execute()
            calls = result.data or []
        except Exception:
            pass

        # Messages (sms en email) uit conversation messages
        sms_messages = []
        emails = []
        try:
            # Haal conversatie IDs
            convs = self.client.table("agent_conversations").select("id").eq(
                "user_id", user_db_id
            ).gte("started_at", week_ago).execute()
            conv_ids = [c["id"] for c in (convs.data or [])]

            if conv_ids:
                msgs = self.client.table("agent_messages").select("*").in_(
                    "conversation_id", conv_ids
                ).eq("role", "assistant").gte("created_at", week_ago).execute()

                for m in (msgs.data or []):
                    content = m.get("content", "")
                    meta = m.get("metadata") or {}
                    if meta.get("type") == "sms" or "[SMS verstuurd]" in content:
                        sms_messages.append(m)
                    elif meta.get("type") == "email" or "[E-mail verstuurd]" in content:
                        emails.append(m)
        except Exception:
            pass

        # Reminders
        reminders = []
        try:
            result = self.client.table("agent_reminders").select("*").eq(
                "user_id", user_db_id
            ).gte("created_at", week_ago).order("remind_at").execute()
            reminders = result.data or []
        except Exception:
            pass

        return {
            "calls": calls,
            "sms_messages": sms_messages,
            "emails": emails,
            "reminders": reminders,
        }

    # === CONTEXT BUILDING ===

    async def get_user_context(self, telegram_id: str) -> Dict[str, Any]:
        """
        Bouw volledige context voor een gebruiker.
        
        Returns:
            Dict met user info, preferences, recent messages, en memories
        """
        user = await self.get_or_create_user(telegram_id)
        
        # Haal recente berichten
        recent_messages = await self.get_recent_messages(telegram_id, limit=5)
        
        # Haal belangrijke herinneringen
        memories = await self.get_memories(telegram_id, limit=10)
        
        # Haal voorkeuren
        preferences = user.get("preferences", {})
        
        return {
            "user": {
                "id": user["id"],
                "telegram_id": telegram_id,
                "name": user.get("first_name") or user.get("telegram_username") or "Gebruiker",
                "full_name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
            },
            "preferences": preferences,
            "recent_messages": [
                {"role": m["role"], "content": m["content"]}
                for m in recent_messages
            ],
            "memories": [
                {"type": m["memory_type"], "content": m["content"]}
                for m in memories
            ]
        }


# Singleton instance
_memory: Optional[MemorySystem] = None


def get_memory_system() -> MemorySystem:
    """Get or create the memory system instance"""
    global _memory
    if _memory is None:
        _memory = MemorySystem()
    return _memory
