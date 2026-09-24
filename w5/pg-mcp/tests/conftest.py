"""Pytest configuration and shared fixtures.

This module provides shared fixtures and configuration for all tests.

Integration and e2e tests use the ``pg_test_env`` fixture (applied via
``pytestmark``) which points Settings at the local PostgreSQL fixture
databases, and the ``mock_llm`` fixture which replaces the LLM services
with deterministic fakes so no OpenAI API access is required.
"""

import os

import pytest

from pg_mcp.config.settings import reset_settings

# Local fixture databases created for the test environment
TEST_DATABASE = os.environ.get("PGMCP_TEST_DB", "blog_small")
TEST_EXTRA_DATABASE = os.environ.get("PGMCP_TEST_EXTRA_DB", "ecommerce_medium")


@pytest.fixture(autouse=True)
def reset_config() -> None:
    """Reset global settings before each test."""
    reset_settings()


@pytest.fixture(autouse=True)
def disable_metrics_for_tests():
    """Disable metrics for tests to avoid port conflicts."""
    os.environ["OBSERVABILITY_METRICS_ENABLED"] = "false"
    yield
    # Clean up
    if "OBSERVABILITY_METRICS_ENABLED" in os.environ:
        del os.environ["OBSERVABILITY_METRICS_ENABLED"]


@pytest.fixture
def pg_test_env(monkeypatch):
    """Configure Settings for the local PostgreSQL fixture databases.

    Applies the environment variables that nested pydantic-settings
    configs read (via their env prefixes) so that lifespan can start
    against the local database without a real OpenAI API key.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key-for-tests-12345")
    monkeypatch.setenv("DATABASE_HOST", "localhost")
    monkeypatch.setenv("DATABASE_PORT", "5432")
    monkeypatch.setenv("DATABASE_NAME", TEST_DATABASE)
    monkeypatch.setenv("DATABASE_USER", "postgres")
    monkeypatch.setenv("DATABASE_PASSWORD", "postgres")
    # Keep retry backoff short so rejection paths stay fast
    monkeypatch.setenv("RESILIENCE_MAX_RETRIES", "2")
    monkeypatch.setenv("RESILIENCE_RETRY_DELAY", "0.1")
    # Tests opt in to extra databases explicitly (see multi-database tests)
    monkeypatch.delenv("EXTRA_DATABASES", raising=False)


def _fake_sql_for(question: str) -> str:
    """Deterministic fake SQL generation.

    SQL-like questions (starting with a statement keyword) are echoed back
    so security rejection paths are exercised realistically; anything else
    gets a benign generic SELECT that always executes.
    """
    q = question.strip()
    first_word = q.split(None, 1)[0].upper() if q else ""
    statement_keywords = {
        "SELECT",
        "WITH",
        "EXPLAIN",
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "CREATE",
        "ALTER",
        "TRUNCATE",
        "GRANT",
    }
    if first_word in statement_keywords:
        return q.rstrip().rstrip(";") + ";"
    return "SELECT * FROM information_schema.tables LIMIT 5;"


@pytest.fixture
def mock_llm(monkeypatch):
    """Replace LLM services with deterministic fakes (no network access).

    - ``SQLGenerator.generate`` echoes SQL-like questions or returns a
      generic SELECT, and records a fixed token count.
    - ``ResultValidator.validate`` always returns confidence 95.
    """
    from pg_mcp.models.query import ResultValidationResult
    from pg_mcp.services.result_validator import ResultValidator
    from pg_mcp.services.sql_generator import SQLGenerator

    async def fake_generate(
        self,
        question: str,
        schema,
        context: str | None = None,
        previous_attempt: str | None = None,
        error_feedback: str | None = None,
    ) -> str:
        self.last_tokens_used = 42
        return _fake_sql_for(question)

    async def fake_validate(
        self, question: str, sql: str, results, row_count: int
    ) -> ResultValidationResult:
        return ResultValidationResult(
            confidence=95,
            explanation="Results match the question (mock validation)",
            suggestion=None,
            is_acceptable=True,
        )

    monkeypatch.setattr(SQLGenerator, "generate", fake_generate)
    monkeypatch.setattr(ResultValidator, "validate", fake_validate)


@pytest.fixture
def mock_llm_with_pg_env(pg_test_env, mock_llm):
    """Combined fixture: fixture database env + mocked LLM services."""
