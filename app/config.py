from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql+asyncpg://mega:mega_secret@localhost:5432/mega_ai"
    database_url_sync: str = "postgresql://mega:mega_secret@localhost:5432/mega_ai"

    # LLM
    groq_api_key: str = ""
    default_model: str = "llama-3.3-70b-versatile"
    fallback_model: str = "llama-3.1-8b-instant"

    # Context budgets (tokens)
    orchestrator_budget: int = 2000
    decomposition_budget: int = 1500
    retrieval_budget: int = 3000
    critique_budget: int = 2000
    synthesis_budget: int = 3000
    compression_budget: int = 1000
    meta_budget: int = 2000

    # Tool configs
    tool_timeout_seconds: int = 30
    tool_max_retries: int = 2

    # General
    log_level: str = "INFO"
    environment: str = "development"

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
