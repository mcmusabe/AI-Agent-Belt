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
    
    # Server
    host: str = Field("0.0.0.0", description="Server host")
    port: int = Field(8000, description="Server port")
    debug: bool = Field(True, description="Debug mode")
    
    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore"
    }


@lru_cache()
def get_settings() -> Settings:
    """Cached settings instance"""
    return Settings()
