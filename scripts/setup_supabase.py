#!/usr/bin/env python3
"""
Setup script om de agent_calls tabel aan te maken in Supabase.
Run dit eenmalig: python scripts/setup_supabase.py
"""
import os
import sys
from pathlib import Path

# Voeg src toe aan path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import httpx

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")

def check_table_exists():
    """Check of agent_calls tabel al bestaat"""
    response = httpx.get(
        f"{SUPABASE_URL}/rest/v1/agent_calls?select=id&limit=1",
        headers={
            "apikey": SUPABASE_ANON_KEY,
            "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
        }
    )
    return response.status_code != 404

def main():
    print("=" * 50)
    print("Supabase Setup voor Connect Smart")
    print("=" * 50)

    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        print("❌ SUPABASE_URL of SUPABASE_ANON_KEY niet gevonden in .env")
        return

    print(f"📡 Supabase URL: {SUPABASE_URL}")

    # Check of tabel bestaat
    if check_table_exists():
        print("✅ agent_calls tabel bestaat al!")
    else:
        print("⚠️  agent_calls tabel bestaat nog niet.")
        print()
        print("Voer de volgende stappen uit:")
        print("1. Ga naar https://supabase.com/dashboard")
        print("2. Open je project: kvcfjultqlxftokyxztj")
        print("3. Ga naar SQL Editor")
        print("4. Plak de inhoud van scripts/create_calls_table.sql")
        print("5. Klik op 'Run'")
        print()
        print("Of gebruik de Supabase CLI:")
        print("  supabase db push")

    # Test bestaande tabellen
    print()
    print("Checking bestaande tabellen...")

    tables = ["agent_users", "agent_conversations", "agent_messages", "agent_memory", "agent_contacts", "agent_reminders"]

    for table in tables:
        response = httpx.get(
            f"{SUPABASE_URL}/rest/v1/{table}?select=id&limit=1",
            headers={
                "apikey": SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
            }
        )
        status = "✅" if response.status_code == 200 else "❌"
        print(f"  {status} {table}")

if __name__ == "__main__":
    main()
