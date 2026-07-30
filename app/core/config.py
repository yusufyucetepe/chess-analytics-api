"""Application settings, loaded from the environment."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Chess Wrapped"
    debug: bool = False

    # Postgres. asyncpg driver for the app, psycopg-free sync URL derived for Alembic.
    database_url: str = "postgresql+asyncpg://chess:chess@localhost:5432/chess"
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 5

    redis_url: str = "redis://localhost:6379/0"

    # Lichess
    lichess_base_url: str = "https://lichess.org"
    lichess_token: str | None = None
    # Lichess asks for a descriptive UA with a way to contact the operator.
    lichess_user_agent: str = "chess-wrapped/0.1 (+https://github.com/yusuf/chess-wrapped)"
    lichess_timeout_s: float = 30.0
    lichess_stream_timeout_s: float = 600.0
    lichess_max_retries: int = 4

    # Report window and ingest
    report_window_days: int = 365
    ingest_batch_size: int = 500
    opening_line_plies: int = 12
    #: Below this many analysed games the quality section degrades instead of
    #: reporting numbers that would be dominated by self-selection bias.
    quality_min_analysed_games: int = 20

    # Caching / dedupe / limits
    report_fresh_ttl_s: int = 24 * 3600
    report_cache_ttl_s: int = 3600
    ingest_lock_ttl_s: int = 1800
    rate_limit_per_hour: int = 10

    cors_origins: list[str] = Field(default_factory=list)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
