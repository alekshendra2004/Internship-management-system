"""Application configuration loaded from environment variables."""
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings


ROOT_DIR = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    app_name: str = "Internship Management System"
    environment: str = "development"
    debug: bool = False

    database_url: str = "postgresql+asyncpg://postgres:postgres@db:5432/internship"
    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_pool_recycle: int = 3600

    jwt_secret: str = "development-only-secret-change-before-production"
    jwt_refresh_secret: str = "development-only-refresh-secret-change-before-production"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 15
    refresh_token_days: int = 7

    frontend_url: str = "http://localhost:5173"
    cors_origins: list[str] = ["http://localhost:5173"]

    storage_provider: str = "local"
    storage_bucket: str = "internship-files"
    storage_access_key: str = ""
    storage_secret_key: str = ""
    storage_endpoint_url: str = ""
    storage_region: str = ""
    storage_auto_create_bucket: bool = False
    local_storage_dir: str = "./data/files"
    public_base_url: str = "http://localhost:8000"

    email_api_key: str = ""
    email_from: str = "noreply@example.com"

    max_upload_mb: int = 25
    seed_admin_email: str = ""
    seed_admin_password: str = ""

    # Redis backs the cache, rate-limit counters and the RQ job queue. Empty = disabled:
    # cache becomes a no-op, rate limits use process memory, and jobs run inline.
    redis_url: str = ""
    job_backend: str = ""  # "rq" | "inline"; empty = "rq" when redis_url is set
    job_queue: str = "default"
    job_timeout_seconds: int = 900
    dashboard_cache_seconds: int = 30

    rate_limit_enabled: bool = True
    rate_limit_requests: int = 100
    rate_limit_window_seconds: int = 60
    login_rate_limit: str = "10/minute"
    login_email_rate_limit: str = "5/minute"
    refresh_rate_limit: str = "30/minute"
    upload_rate_limit: str = "30/minute"
    export_rate_limit: str = "10/minute"
    import_rate_limit: str = "10/minute"

    import_max_rows: int = 5000
    import_max_mb: int = 2
    import_inline_rows: int = 200

    log_level: str = "INFO"
    log_json: bool = True

    @field_validator("database_url", mode="before")
    @classmethod
    def use_asyncpg_driver(cls, value: object) -> object:
        """Accept Neon's standard PostgreSQL URL with this async SQLAlchemy app."""
        if isinstance(value, str) and value.startswith("postgresql://"):
            return "postgresql+asyncpg://" + value.removeprefix("postgresql://")
        return value

    # Neon CLI writes credentials to the repository-level .env.local. Keep the
    # backend-local .env supported for existing Docker and local workflows.
    model_config = {
        "env_file": (ROOT_DIR / ".env.local", ROOT_DIR / ".env", ".env"),
        "extra": "ignore",
    }

    @property
    def effective_job_backend(self) -> str:
        if self.job_backend:
            return self.job_backend
        return "rq" if self.redis_url else "inline"


@lru_cache
def get_settings() -> Settings:
    return Settings()
