"""
CodeSentinel — Application Configuration
Loads from .env via Pydantic Settings.
"""
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ──────────────────────────────────────────────────────────────────
    app_env: Literal["development", "production"] = "development"
    log_level: str = "INFO"
    secret_key: str = "change_me_in_production"

    # ── Groq / LLM ───────────────────────────────────────────────────────────
    groq_api_key: str = Field(..., description="Groq API key")
    groq_model: str = "llama-3.3-70b-versatile"
    groq_max_tokens: int = 4096
    groq_temperature: float = 0.1

    # ── GitHub ───────────────────────────────────────────────────────────────
    github_token: str = Field(..., description="GitHub PAT with PR write + contents read")
    github_webhook_secret: str = Field(..., description="HMAC-SHA256 webhook secret")

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── Rate Limiting ─────────────────────────────────────────────────────────
    webhook_rate_limit: str = "100/minute"

    # ── Review Thresholds ─────────────────────────────────────────────────────
    critic_confidence_threshold: float = 0.6
    max_findings_per_review: int = 50

    # ── Phase 3: Timeout Budgets (seconds) ───────────────────────────────────
    # Per-agent timeout: each specialist agent gets this long to call Groq
    agent_timeout_s: float = 45.0
    # Critic node timeout: second LLM pass to filter false positives
    critic_timeout_s: float = 30.0
    # Total graph timeout: hard cap on the entire multi-agent review pipeline
    graph_timeout_s: float = 120.0

    # ── Phase 4: Chroma Vector Store (Memory) ────────────────────────────────
    chroma_persist_dir: str = "./.chroma_data"
    chroma_similarity_threshold: float = 0.95
    chroma_embedding_model: str = "all-MiniLM-L6-v2"

    # ── Phase 3: Circuit Breaker ──────────────────────────────────────────────
    # How many consecutive failures before a node's circuit opens
    cb_failure_threshold: int = 5
    # Seconds the circuit stays OPEN before probing again (half-open)
    cb_recovery_timeout_s: float = 60.0

    # ── LangSmith (optional) ──────────────────────────────────────────────────
    langchain_tracing_v2: bool = False
    langchain_api_key: str = ""
    langchain_project: str = "codesentinel"

    @field_validator("groq_api_key", "github_token", "github_webhook_secret", mode="before")
    @classmethod
    def must_not_be_empty(cls, v: str, info) -> str:  # noqa: N805
        placeholder_prefixes = ("your_", "change_me", "<", "")
        v = v.strip() if v else v
        if not v or any(v.startswith(p) for p in placeholder_prefixes if p):
            raise ValueError(
                f"{info.field_name} must be set in .env — "
                "see .env.example for instructions"
            )
        return v

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton. Use this everywhere instead of instantiating Settings()."""
    return Settings()
