from dataclasses import dataclass
import os
from typing import Optional


def _get_env(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value


def _get_optional_env(name: str) -> Optional[str]:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return None
    return value.strip()


def _parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    app_env: str = "local"
    app_name: str = "Jolly"
    app_version: str = "0.2.0"
    log_level: str = "INFO"
    cors_origins: tuple[str, ...] = ("http://localhost:5173", "http://localhost:4173")
    database_url: Optional[str] = None
    llm_api_key: Optional[str] = None
    embedding_api_key: Optional[str] = None
    # Google AI Studio free tier; no billing needed.
    llm_model: str = "gemini-3.8-flash"
    llm_fallback_models: tuple[str, ...] = ()
    embedding_model: str = "gemini-embedding-2"
    # Output size for Gemini embeddings (768 fits a pgvector vector(768) column).
    embedding_dim: int = 768
    storage_bucket: Optional[str] = None
    vector_database_url: Optional[str] = None
    # Bolna voice layer (https://www.bolna.ai/docs/api-reference/introduction)
    bolna_api_key: Optional[str] = None
    bolna_base_url: str = "https://api.bolna.ai"
    bolna_agent_id: Optional[str] = None
    bolna_from_number: Optional[str] = None
    # Shared secret Bolna sends back to us (query param or header) on
    # webhooks and custom-function tool calls. Optional for local dev.
    bolna_webhook_secret: Optional[str] = None
    # Optional allowlist for POST /call (comma-separated E.164).
    call_allowed_numbers: tuple[str, ...] = ()
    # Load the synthetic demo matters into an empty database on startup.
    seed_demo_data: bool = True
    # Trust layer. HMAC key for signed approval links (falls back to
    # BOLNA_WEBHOOK_SECRET); token lifetime; pepper for hashing phone numbers
    # in the audit log (falls back to the approval key).
    approval_secret: Optional[str] = None
    approval_ttl_seconds: int = 86400
    approval_token_in_payload: bool = True
    audit_pepper: Optional[str] = None


def get_settings() -> Settings:
    cors_origins = _parse_csv(
        _get_env("CORS_ORIGINS", "http://localhost:5173,http://localhost:4173")
    )

    return Settings(
        app_env=_get_env("APP_ENV", "local"),
        app_name=_get_env("APP_NAME", "Jolly"),
        app_version=_get_env("APP_VERSION", "0.2.0"),
        log_level=_get_env("LOG_LEVEL", "INFO").upper(),
        cors_origins=tuple(cors_origins),
        database_url=_get_optional_env("DATABASE_URL"),
        llm_api_key=_get_optional_env("LLM_API_KEY"),
        embedding_api_key=_get_optional_env("EMBEDDING_API_KEY") or _get_optional_env("LLM_API_KEY"),
        llm_model=_get_env("LLM_MODEL", "gemini-3.8-flash"),
        llm_fallback_models=tuple(_parse_csv(_get_env("LLM_FALLBACK_MODELS", "gemini-flash-lite-latest,gemini-2.5-flash-lite,gemini-flash-latest,gemini-2.5-flash"))),
        embedding_model=_get_env("EMBEDDING_MODEL", "gemini-embedding-2"),
        embedding_dim=int(_get_env("EMBEDDING_DIM", "768")),
        storage_bucket=_get_optional_env("STORAGE_BUCKET"),
        vector_database_url=_get_optional_env("VECTOR_DATABASE_URL"),
        bolna_api_key=_get_optional_env("BOLNA_API_KEY"),
        bolna_base_url=_get_env("BOLNA_BASE_URL", "https://api.bolna.ai").rstrip("/"),
        bolna_agent_id=_get_optional_env("BOLNA_AGENT_ID"),
        bolna_from_number=_get_optional_env("BOLNA_FROM_NUMBER"),
        bolna_webhook_secret=_get_optional_env("BOLNA_WEBHOOK_SECRET"),
        call_allowed_numbers=tuple(_parse_csv(_get_env("CALL_ALLOWED_NUMBERS", ""))),
        seed_demo_data=_get_env("SEED_DEMO_DATA", "true").lower() in ("1", "true", "yes"),
        approval_secret=_get_optional_env("APPROVAL_SECRET"),
        approval_ttl_seconds=int(_get_env("APPROVAL_TTL_SECONDS", "86400")),
        approval_token_in_payload=_get_env("APPROVAL_TOKEN_IN_PAYLOAD", "true").lower() in ("1", "true", "yes"),
        audit_pepper=_get_optional_env("AUDIT_PEPPER"),
    )
