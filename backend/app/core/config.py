from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    cors_origins: list[str] = ["http://localhost:3010"]

    database_backend: Literal["supabase", "memory"] = "memory"
    supabase_url: str = ""
    supabase_service_role_key: str = ""

    llm_provider: Literal["ollama", "bedrock", "fake"] = "ollama"
    ollama_model: str = "qwen3.5:latest"
    aws_region: str = "eu-central-1"
    bedrock_model_id: str = ""

    voice_provider: Literal["browser_dev", "elevenlabs", "nova_sonic"] = "browser_dev"
    elevenlabs_api_key: str = ""
    elevenlabs_webhook_secret: str = ""
    tool_webhook_secret: str = ""

    firehose_stream: str = ""  # empty = events go to the log only


@lru_cache
def get_settings() -> Settings:
    return Settings()
