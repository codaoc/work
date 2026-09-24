# pg-mcp 正常使用界面示例

本文档展示一次**真实运行**的完整会话记录，供评阅与使用参考。

- 运行时间：2026-09-24，环境：Fedora + PostgreSQL 18，fixture 库 `blog_small`（10 张表，8 个用户，10 篇文章）
- 运行方式：`uv run python demo/run_demo.py`（真实 MCP stdio 协议 + 真实数据库执行 + 真实安全校验；仅 LLM 生成/结果校验两步为确定性 mock，因演示环境无 OPENAI_API_KEY）
- 所有输出均来自实际运行捕获，非手工构造。

---

## 1. 启动服务（终端视角）

```console
$ ./run.sh        # 需要 OPENAI_API_KEY；演示环境改用 demo/server_with_mock_llm.py
```

服务端启动阶段在 **stderr** 输出结构化日志（stdout 专用于 MCP JSON-RPC 通道）：

```json
{"timestamp": "2026-09-24T11:49:06", "level": "INFO", "logger": "pg_mcp.server", "message": "Configuration loaded", "extra": {"environment": "development", "log_level": "INFO", "databases": ["blog_small"]}}
{"timestamp": "2026-09-24T11:49:06", "level": "INFO", "logger": "pg_mcp.server", "message": "Created connection pool for database 'blog_small'", "extra": {"min_size": 5, "max_size": 20}}
{"timestamp": "2026-09-24T11:49:07", "level": "INFO", "logger": "pg_mcp.server", "message": "Schema loaded for 'blog_small'", "extra": {"tables": 10}}
{"timestamp": "2026-09-24T11:49:07", "level": "INFO", "logger": "pg_mcp.server", "message": "Metrics server started on port 9090"}
{"timestamp": "2026-09-24T11:49:07", "level": "INFO", "logger": "pg_mcp.server", "message": "Server ready to accept requests", "extra": {"databases": ["blog_small"], "cache_enabled": true, "metrics_enabled": true}}
```

---

## 2. 用户视角（MCP 宿主如 Claude Desktop 中的对话效果）

> **用户**：数据库里一共有多少个用户？

> **助手**：调用 `query` 工具 → 数据库中共有 **8 个用户**。

> **用户**：看一下浏览量最高的 5 篇文章

> **助手**：调用 `query` 工具 → 浏览量 Top 5 如下：
>
> | id | 标题 | 浏览量 |
> |----|------|--------|
> | 2  | Top 10 Travel Destinations in 2024 | 2341 |
> | 9  | Mindfulness in Daily Life          | 2100 |
> | 10 | Remote Work Best Practices         | 1876 |
> | 4  | Time Management Tips for Developers| 1654 |
> | 1  | Getting Started with Python        | 1523 |

> **用户**：只要 SQL，不要执行：列出一些用户

> **助手**：调用 `query`（`return_type="sql"`）→ 生成的 SQL 为
> `SELECT COUNT(*) AS row_count FROM users`（未执行）。

---

## 3. 底层 MCP 工具调用（实际 JSON 报文）

MCP 宿主与服务端之间的实际交互（`result.content[0].text` 内容，真实捕获）：

### 3.1 健康检查 `health`

```json
{
  "status": "healthy",
  "databases": ["blog_small"],
  "schema_cache": {"cached": ["blog_small"]},
  "circuit_breaker": {"state": "closed", "failure_count": 0, "failure_threshold": 5,
                      "recovery_timeout": 60.0, "last_failure_time": null},
  "rate_limiter": {
    "queries": {"max_concurrent": 10, "active_count": 0, "available": 10,
                "total_requests": 0, "total_rejections": 0},
    "llm":     {"max_concurrent": 5,  "active_count": 0, "available": 5,
                "total_requests": 0, "total_rejections": 0}
  }
}
```

### 3.2 自然语言查询

**请求** `query`：

```json
{"question": "How many users are there in total?"}
```

**响应**：

```json
{
  "success": true,
  "generated_sql": "SELECT COUNT(*) AS user_count FROM users",
  "validation": {
    "is_valid": true,
    "is_select": true,
    "allows_data_modification": false,
    "uses_blocked_functions": [],
    "error_message": null
  },
  "data": {
    "columns": ["user_count"],
    "rows": [{"user_count": 8}],
    "row_count": 1,
    "execution_time_ms": 3.19
  },
  "error": null,
  "confidence": 95,
  "tokens_used": 128,
  "warning": null
}
```

**请求**（Top 5 文章）：

```json
{"question": "Show me the top 5 posts by view count"}
```

**响应**（节选 `data`）：

```json
{
  "columns": ["id", "title", "view_count"],
  "rows": [
    {"id": 2,  "title": "Top 10 Travel Destinations in 2024",  "view_count": 2341},
    {"id": 9,  "title": "Mindfulness in Daily Life",           "view_count": 2100},
    {"id": 10, "title": "Remote Work Best Practices",          "view_count": 1876},
    {"id": 4,  "title": "Time Management Tips for Developers", "view_count": 1654},
    {"id": 1,  "title": "Getting Started with Python",         "view_count": 1523}
  ],
  "row_count": 5,
  "execution_time_ms": 2.95
}
```

### 3.3 仅生成 SQL（不执行）

**请求**：

```json
{"question": "List some users", "return_type": "sql"}
```

**响应**（注意 `data` 为 `null`，`confidence` 为 100）：

```json
{
  "success": true,
  "generated_sql": "SELECT COUNT(*) AS row_count FROM users",
  "validation": {"is_valid": true, "is_select": true,
                 "allows_data_modification": false, "uses_blocked_functions": [],
                 "error_message": null},
  "data": null,
  "error": null,
  "confidence": 100,
  "tokens_used": 128,
  "warning": null
}
```

### 3.4 正常使用中的安全拦截（附例）

用户要求执行危险操作时，安全校验（真实 pglast 解析 + 规则校验，非 mock）会拒绝：

**请求**：`{"question": "DELETE FROM users"}`
**响应**：

```json
{
  "success": false,
  "error": {
    "code": "security_violation",
    "message": "DELETE statements are not allowed. Only SELECT queries are permitted.",
    "details": {}
  },
  "confidence": 0,
  "tokens_used": 0,
  "warning": null
}
```

---

## 4. 服务端请求期日志

每次查询在 stderr 输出带 `request_id` 的全链路日志（节选，真实捕获）：

```json
{"level": "DEBUG", "logger": "pg_mcp.services.orchestrator", "message": "Generating SQL", "extra": {"request_id": "8f14…", "attempt": 1, "max_retries": 2}}
{"level": "DEBUG", "logger": "pg_mcp.services.orchestrator", "message": "SQL generated", "extra": {"request_id": "8f14…", "sql_length": 42}}
{"level": "INFO",  "logger": "pg_mcp.services.orchestrator", "message": "SQL generated and validated successfully", "extra": {"request_id": "8f14…", "attempts": 1}}
{"level": "INFO",  "logger": "pg_mcp.services.orchestrator", "message": "SQL executed successfully", "extra": {"request_id": "8f14…", "database": "blog_small", "row_count": 1, "execution_time_ms": 3.19}}
{"level": "INFO",  "logger": "pg_mcp.services.orchestrator", "message": "Result validation completed", "extra": {"request_id": "8f14…", "confidence": 95, "is_acceptable": true}}
```

---

## 5. Prometheus 指标端点

服务运行期间 `curl localhost:9090/metrics` 可抓取（节选）：

```text
pg_mcp_query_duration_seconds_bucket{le="0.1"} 0.0
pg_mcp_query_duration_seconds_bucket{le="0.5"} 0.0
pg_mcp_query_duration_seconds_bucket{le="1.0"} 0.0
...
```

---

## 复现方式

```bash
cd ~/work/w5/pg-mcp
uv run python demo/run_demo.py      # 9 个用例（含成功路径与安全/校验拒绝路径）
```

LLM mock 规则（demo/server_with_mock_llm.py）：自然语言问题按关键词映射到确定性 SQL；以 SQL 语句开头的问题原样回传，从而让真实安全校验器处理 `DELETE`、`pg_sleep` 等危险输入。
