#!/usr/bin/env python3
"""
Eenmalig script: update de Vapi assistant naar Connect Smart branding.
Voer uit vanaf de projectroot: python scripts/update_vapi_assistant.py
"""
import asyncio
import sys
from pathlib import Path

# Zorg dat src importeerbaar is
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agents.voice import VoiceAgent


async def main():
    agent = VoiceAgent()
    print("Vapi assistant bijwerken naar Connect Smart - Sophie...")
    result = await agent.update_assistant_to_connect_smart()
    if result.get("success"):
        print("OK – Assistant heet nu 'Connect Smart - Sophie', endCallMessage bijgewerkt.")
        return 0
    print("Fout:", result.get("error", "onbekend"), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
