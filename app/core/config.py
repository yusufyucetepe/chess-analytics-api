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
    #: Per-move engine evals in the game export. Roughly triples the payload and
    #: no v1 report section reads it, but it is the only source of the eval
    #: swings a later puzzle feature needs to find tactical positions -- so it
    #: stays on, and ingest is what decides how much of it to keep.
    lichess_include_evals: bool = True

    # Report window and ingest
    report_window_days: int = 365
    ingest_batch_size: int = 500
    opening_line_plies: int = 12
    #: Below this many analysed games the quality section degrades instead of
    #: reporting numbers that would be dominated by self-selection bias.
    quality_min_analysed_games: int = 20

    # Report sections
    report_top_openings: int = 5
    #: How deep the repertoire tree goes. Bounded by `opening_line_plies`, since
    #: that is all ingest kept.
    report_tree_plies: int = 8
    #: Lines played once are noise in a tree and most of its JSON size.
    report_tree_min_games: int = 2
    #: An opening needs this many games before its score is worth ranking on.
    report_opening_min_games: int = 5
    #: A gap longer than this ends a playing session.
    report_session_gap_s: int = 30 * 60
    #: Ceiling on one game's contribution to hours played. Correspondence games
    #: run for days of wall-clock time the player did not spend at the board.
    report_max_game_s: int = 4 * 3600

    # Worker
    #: How many times arq may run one report job. Only network faults get a
    #: second attempt; a username that doesn't exist fails on the first.
    worker_max_tries: int = 3
    #: A year of bullet is tens of thousands of games and the export is rate
    #: limited, so the default 5 minutes is nowhere near enough.
    worker_job_timeout_s: int = 30 * 60
    worker_keep_result_s: int = 3600
    #: Base wait before a retried report job runs again, multiplied by the
    #: attempt number. Lichess rate limits are measured in minutes, so coming
    #: straight back would just spend the next attempt on the same 429.
    worker_retry_delay_s: int = 30

    # Caching / dedupe / limits
    #: Below this age a report is served as is, whatever has happened since.
    #: The floor on how often one player can cause an export.
    report_fresh_ttl_s: int = 24 * 3600
    #: Above this age a report is rebuilt even when the player has played
    #: nothing new. Between the two, the rated-game count decides. The ceiling
    #: exists because the count only moves when games are *played*: a player who
    #: goes back and analyses old games changes what the quality section would
    #: say without changing the number we compare against.
    report_stale_ttl_s: int = 7 * 24 * 3600
    report_cache_ttl_s: int = 3600
    ingest_lock_ttl_s: int = 1800
    #: Reports one address may ask to have built per hour. Zero disables it.
    rate_limit_per_hour: int = 10
    #: Reads that recompute rather than serve a stored row -- the openings tree
    #: runs several aggregates over a player's whole year, so it needs a ceiling
    #: of its own. Far looser than the write limit: the work is bounded and the
    #: only thing at stake is our own database. Zero disables it.
    rate_limit_reads_per_hour: int = 120

    cors_origins: list[str] = Field(default_factory=list)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
