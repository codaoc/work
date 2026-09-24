"""MCP stdio client that drives the pg-mcp server end to end.

Spawns demo/server_with_mock_llm.py as a subprocess (exactly like a real
MCP host would), performs the MCP handshake, then runs a suite of demo
test cases: tool discovery, health, successful natural-language queries,
SQL-only mode, security rejections, and input validation errors.

Usage: uv run python demo/run_demo.py
"""

import asyncio
import json
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def show(title: str, response: dict, fields: tuple[str, ...] = ()) -> None:
    """Print one demo case result concisely."""
    print(f"\n### {title}")
    if response.get("success"):
        print(f"    success=True  confidence={response.get('confidence')}"
              f"  tokens_used={response.get('tokens_used')}")
        if response.get("generated_sql"):
            print(f"    sql: {response['generated_sql']}")
        data = response.get("data")
        if data:
            print(f"    columns: {data['columns']}  rows_returned={data['row_count']}")
            for row in data["rows"][:3]:
                print(f"      {row}")
            if data["row_count"] > 3:
                print(f"      ... ({data['row_count']} rows total)")
        if response.get("warning"):
            print(f"    warning: {response['warning']}")
    else:
        err = response.get("error") or {}
        print(f"    success=False  code={err.get('code')}")
        print(f"    message: {err.get('message')}")
    for field in fields:
        print(f"    {field}: {response.get(field)}")


async def call(session: ClientSession, arguments: dict) -> dict:
    result = await session.call_tool("query", arguments)
    return json.loads(result.content[0].text)


async def main() -> None:
    params = StdioServerParameters(
        command="uv",
        args=["run", "python", "demo/server_with_mock_llm.py"],
        cwd=str(PROJECT_ROOT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            print("=== 1. MCP handshake & tool discovery ===")
            tools = await session.list_tools()
            print(f"    tools: {[t.name for t in tools.tools]}")

            print("\n=== 2. health tool ===")
            health = await session.call_tool("health", {})
            print(f"    {json.loads(health.content[0].text)}")

            print("\n=== 3. query: natural language -> result ===")
            show("How many users?",
                 await call(session, {"question": "How many users are there in total?"}))
            show("Top 5 posts by views",
                 await call(session, {"question": "Show me the top 5 posts by view count"}))

            print("\n=== 4. query: return_type='sql' (no execution) ===")
            show("SQL only",
                 await call(session, {"question": "List some users", "return_type": "sql"}))

            print("\n=== 5. security: blocked function (real validator) ===")
            show("pg_sleep rejected",
                 await call(session, {"question": "SELECT pg_sleep(10)"}))

            print("\n=== 6. security: write statement rejected ===")
            show("DELETE rejected",
                 await call(session, {"question": "DELETE FROM users"}))

            print("\n=== 7. validation: unknown database ===")
            show("Unknown DB",
                 await call(session, {"question": "How many users?", "database": "no_such_db"}))

            print("\n=== 8. validation: question too long ===")
            show("Overlong question",
                 await call(session, {"question": "a" * 60}))

            print("\n=== 9. validation: bad return_type (tool-level) ===")
            bad = await session.call_tool(
                "query", {"question": "How many users?", "return_type": "bogus"})
            show("Invalid return_type", json.loads(bad.content[0].text))


if __name__ == "__main__":
    asyncio.run(main())
