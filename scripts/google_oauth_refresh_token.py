#!/usr/bin/env python3
"""
Helper: generate a Google OAuth refresh token for Gmail + Calendar.

Requirements:
  pip install -r requirements.txt

Usage:
  GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... python scripts/google_oauth_refresh_token.py
  # or set them in .env
"""
import os
import sys

from dotenv import load_dotenv


def main() -> int:
    load_dotenv(dotenv_path=".env")

    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()

    if not client_id or not client_secret:
        print("Missing GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET in .env or env vars.")
        return 1

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Missing google-auth-oauthlib. Install requirements.txt first.")
        return 1

    scopes = [
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/calendar.events",
    ]

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, scopes=scopes)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    if not creds.refresh_token:
        print("No refresh token returned. Try again with prompt=consent.")
        return 1

    print("REFRESH_TOKEN=" + creds.refresh_token)
    print("Add this to your .env as GOOGLE_REFRESH_TOKEN=...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
