"""
Configuratie en environment variabelen voor Connect Smart.
"""
from pydantic_settings import BaseSettings
from pydantic import Field
from functools import lru_cache


class Settings(BaseSettings):
    """Applicatie-instellingen geladen vanuit .env"""
    
    # Anthropic (Claude)
    anthropic_api_key: str = Field(..., description="Anthropic API key voor Claude")
    
    # Vapi.ai Voice
    vapi_private_key: str = Field(..., description="Vapi private API key")
    vapi_public_key: str = Field("", description="Vapi public API key")
    vapi_assistant_id: str = Field("", description="Vapi assistant ID")
    vapi_phone_number_id: str = Field("", description="Vapi phone number ID for outbound calls")
    vapi_phone_number: str = Field("", description="Vapi phone number")

    # ElevenLabs voice (optioneel): Voice ID uit ElevenLabs Voice Library voor Nederlands accent.
    # API key voor ElevenLabs zet je in het Vapi-dashboard onder Provider Keys.
    # Zet in .env: ELEVENLABS_VOICE_ID=jouw_voice_id
    elevenlabs_voice_id: str = Field(
        default="",
        description="ElevenLabs voice ID (e.g. Dutch accent)",
        validation_alias="ELEVENLABS_VOICE_ID",
    )
    
    # Telegram Bot
    telegram_bot_token: str = Field("", description="Telegram Bot API token")
    
    # WhatsApp Business API
    whatsapp_token: str = Field("", description="WhatsApp Business API token")
    whatsapp_phone_number_id: str = Field("", description="WhatsApp phone number ID")
    whatsapp_verify_token: str = Field("connect-smart-verify", description="Webhook verify token")
    
    # Supabase
    supabase_url: str = Field("", description="Supabase project URL")
    supabase_anon_key: str = Field("", description="Supabase anon key")
    supabase_service_key: str = Field("", description="Supabase service key")
    
    # OpenAI (voor Whisper speech-to-text)
    openai_api_key: str = Field("", description="OpenAI API key voor Whisper")

    # Twilio (optioneel) - voor preflight nummercheck + SMS
    twilio_account_sid: str = Field("", description="Twilio Account SID (optioneel)")
    twilio_auth_token: str = Field("", description="Twilio Auth Token (optioneel)")
    twilio_sms_number: str = Field("", description="Twilio SMS afzendernummer (E.164)")
    twilio_messaging_service_sid: str = Field("", description="Twilio Messaging Service SID (A2P)")
    
    # Server
    host: str = Field("0.0.0.0", description="Server host")
    port: int = Field(8000, description="Server port")
    debug: bool = Field(True, description="Debug mode")
    cors_allow_origins: str = Field(
        "*",
        description="Comma-separated CORS origins, or '*' for all"
    )

    # Google OAuth (Calendar + Gmail)
    google_client_id: str = Field("", description="Google OAuth client ID")
    google_client_secret: str = Field("", description="Google OAuth client secret")
    google_redirect_uri: str = Field("", description="Google OAuth redirect URI")
    google_refresh_token: str = Field("", description="Google OAuth refresh token")
    google_calendar_id: str = Field("primary", description="Google Calendar ID")
    gmail_from_email: str = Field("", description="Default Gmail from address")
    
    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore"
    }


@lru_cache()
def get_settings() -> Settings:
    """Cached settings instance"""
    return Settings()
