from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # LiveKit
    livekit_url: str
    livekit_api_key: str
    livekit_api_secret: str
    sip_outbound_trunk_id: str
    sip_from_number: str = ""               # caller ID shown on outbound calls

    # AI services
    openai_api_key: str
    deepgram_api_key: str

    # Supabase
    supabase_url: str
    supabase_service_key: str               # use service_role key for backend

    # Backend self-reference (agent uses this to POST webhooks back)
    backend_url: str = "http://localhost:8000"

    # Webhook authentication
    webhook_secret: str = ""                    # shared secret between agent and backend

    # Recording storage (Supabase)
    supabase_storage_bucket: str = "call-recordings"
    recordings_dir: str = "./recordings"

    # Call behaviour
    dial_timeout_secs: int = 60             # how long to wait for answer
    max_call_duration_secs: int = 1800      # hard limit: 30 min

    class Config:
        env_file = ".env.local"
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()
