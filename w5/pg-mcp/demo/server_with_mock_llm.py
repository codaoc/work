"""Demo server entry: real pg-mcp MCP server + deterministic LLM mocks.

This wraps the production server (`python -m pg_mcp`) for offline demos.
Everything is real — MCP stdio protocol, connection pools, schema cache,
SQL validation, query execution against PostgreSQL, resilience, metrics —
except the two OpenAI calls (SQLGenerator / ResultValidator), which are
replaced with deterministic fakes so no API key is required.

Run via demo/run_demo.py (which spawns this over stdio like a real host).
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.chdir(PROJECT_ROOT)


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (comments/quotes aware, never overrides real env)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_dotenv(PROJECT_ROOT / ".env")

# Demo overrides:
# - placeholder key (OpenAIConfig requires a non-empty sk-* value; never used
#   because the generator is mocked below)
# - short retry backoff so security-rejection demos stay fast
# - lower question length limit so the QUESTION_TOO_LONG path is reachable
#   (pydantic caps the field itself at 10000, so with the default config the
#   orchestrator check could never trigger first)
os.environ.setdefault("OPENAI_API_KEY", "sk-demo-placeholder-key")
os.environ["RESILIENCE_MAX_RETRIES"] = "1"
os.environ["RESILIENCE_RETRY_DELAY"] = "0.1"  # config floor is 0.1 (ge=0.1)
os.environ["VALIDATION_MAX_QUESTION_LENGTH"] = "50"

from pg_mcp.models.query import ResultValidationResult  # noqa: E402
from pg_mcp.services.result_validator import ResultValidator  # noqa: E402
from pg_mcp.services.sql_generator import SQLGenerator  # noqa: E402

# --- deterministic LLM fakes -------------------------------------------------

STATEMENT_KEYWORDS = {
    "SELECT", "WITH", "EXPLAIN", "INSERT", "UPDATE", "DELETE", "DROP",
    "CREATE", "ALTER", "TRUNCATE", "GRANT",
}

MOCK_SQL_RULES = [
    ("how many users", "SELECT COUNT(*) AS user_count FROM users"),
    ("how many posts", "SELECT COUNT(*) AS post_count FROM posts"),
    ("top 5 posts", "SELECT id, title, view_count FROM posts ORDER BY view_count DESC LIMIT 5"),
    ("list users", "SELECT id, username, email FROM users ORDER BY id LIMIT 5"),
]


async def fake_generate(
    self,
    question: str,
    schema,
    context: str | None = None,
    previous_attempt: str | None = None,
    error_feedback: str | None = None,
) -> str:
    """Return deterministic SQL: echo statement-like input, else rule lookup."""
    self.last_tokens_used = 128
    q = question.strip()
    first = q.split(None, 1)[0].upper() if q else ""
    if first in STATEMENT_KEYWORDS:
        return q  # lets security demos send raw SQL through the real validator
    lowered = q.lower()
    for keyword, sql in MOCK_SQL_RULES:
        if keyword in lowered:
            return sql
    return "SELECT COUNT(*) AS row_count FROM users"


async def fake_validate(self, *, question, sql, results, row_count):
    return ResultValidationResult(
        confidence=95,
        explanation="demo mock: results are consistent with the question",
        suggestion=None,
        is_acceptable=True,
    )


SQLGenerator.generate = fake_generate
ResultValidator.validate = fake_validate

# --- run the real server ------------------------------------------------------

from pg_mcp.server import mcp  # noqa: E402

import anyio  # noqa: E402

if __name__ == "__main__":
    anyio.run(mcp.run_stdio_async)
