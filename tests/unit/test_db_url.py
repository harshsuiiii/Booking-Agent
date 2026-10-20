"""DATABASE_URL handling for hosted Postgres (Supabase) and its redaction in logs."""

import pytest

from booking_agent.db.session import engine_options, normalize_db_url
from booking_agent.observability.redaction import redact_secrets

SESSION_POOLER = (
    "postgresql://postgres.abcref:s3cr3t-pw@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"
)
TXN_POOLER = (
    "postgres://postgres.abcref:s3cr3t-pw@aws-0-ap-south-1.pooler.supabase.com:6543/postgres"
)


class TestNormalize:
    @pytest.mark.parametrize(
        "raw", [SESSION_POOLER, SESSION_POOLER.replace("postgresql", "postgres", 1)]
    )
    def test_supabase_url_gets_the_async_driver(self, raw: str) -> None:
        url = normalize_db_url(raw)
        assert url.startswith("postgresql+asyncpg://postgres.abcref:s3cr3t-pw@")
        assert url.endswith("pooler.supabase.com:5432/postgres")

    def test_sslmode_becomes_asyncpg_ssl(self) -> None:
        url = normalize_db_url(SESSION_POOLER + "?sslmode=require&pgbouncer=true")
        assert "ssl=require" in url and "sslmode" not in url and "pgbouncer" not in url

    def test_password_with_special_characters_survives(self) -> None:
        raw = "postgresql://postgres.ref:p%40ss%2Fword@host.pooler.supabase.com:5432/postgres"
        assert "p%40ss%2Fword@" in normalize_db_url(raw)

    @pytest.mark.parametrize(
        "url",
        [
            "sqlite+aiosqlite:///./data/booking_agent.db",
            "postgresql+asyncpg://booking:booking@localhost:5432/booking_agent",
        ],
    )
    def test_already_async_urls_are_unchanged(self, url: str) -> None:
        assert normalize_db_url(url) == url


class TestEngineOptions:
    def test_session_pooler_keeps_prepared_statements(self) -> None:
        opts = engine_options(normalize_db_url(SESSION_POOLER))
        assert opts["pool_pre_ping"] and opts["pool_recycle"] == 300
        assert "connect_args" not in opts

    def test_transaction_pooler_turns_statement_cache_off(self) -> None:
        opts = engine_options(normalize_db_url(TXN_POOLER))
        assert opts["connect_args"]["statement_cache_size"] == 0
        names = {opts["connect_args"]["prepared_statement_name_func"]() for _ in range(3)}
        assert len(names) == 3  # unique per statement, never reused across pooled sessions

    def test_sqlite_gets_no_pool_sizing(self) -> None:
        assert engine_options("sqlite+aiosqlite:///./x.db") == {"pool_pre_ping": True}


class TestRedaction:
    @pytest.mark.parametrize("url", [SESSION_POOLER, TXN_POOLER, normalize_db_url(SESSION_POOLER)])
    def test_database_password_is_masked(self, url: str) -> None:
        out = redact_secrets(f"connect failed: {url}")
        assert "s3cr3t-pw" not in out
        assert "REDACTED@" in out and "pooler.supabase.com" in out
