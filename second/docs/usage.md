# pg-agent 使用文档

pg-agent 是一个通过自然语言驱动 PostgreSQL 表结构设计的 CLI 智能体：你用中文描述需求，它内省真实数据库、生成 DDL、在真实数据库上试执行验证，经风险分级与人工确认后，以**版本化迁移文件 + 审计台账**的形式落地变更。

核心理念（也是它与你直接让 LLM "生成 SQL 再贴到 psql" 的区别）：

- **数据库是唯一事实源**。智能体每次设计前必须先内省真实结构，不允许凭想象引用不存在的表。
- **一切皆迁移**。每个变更都是一个带时间戳版本号的纯 SQL 文件，可被标准工具重放，同时写入数据库内台账（`agent_schema_migrations`）供审计与对账。
- **LLM 只提议，不裁决**。所有 SQL 经过四层校验（语法 → 白名单 → 风险规则 → 事务内试执行），中高危变更必须经人工确认门控。
- **人类可审计、可回退**。需求摘要、风险、涉及表、完整 SQL 全部落盘（台账 + `SCHEMA_DESIGN.md` 设计日志），可选提供 down migration 并验证其可回滚性。

---

## 目录

1. [安装](#1-安装)
2. [准备数据库与凭证](#2-准备数据库与凭证)
3. [快速上手](#3-快速上手)
4. [CLI 命令参考](#4-cli-命令参考)
5. [会话内交互](#5-会话内交互)
6. [风险分级与确认策略](#6-风险分级与确认策略)
7. [配置参考（pgagent.toml）](#7-配置参考pgagenttoml)
8. [产出物与目录结构](#8-产出物与目录结构)
9. [典型工作流](#9-典型工作流)
10. [安全模型](#10-安全模型)
11. [故障排查](#11-故障排查)
12. [运行测试](#12-运行测试)

---

## 1. 安装

要求：Python ≥ 3.12，可访问的 PostgreSQL 12+ 实例。

```bash
# 克隆或进入项目目录后
python -m venv .venv && source .venv/bin/activate
pip install -e .            # 安装 pgagent 与 CLI
pip install -e ".[dev]"     # 如需运行测试
```

安装后获得命令行入口 `pgagent`（等价于 `python -m` 入口 `.venv/bin/pgagent`）。

智能体对话需要 Anthropic API 凭证，通过标准环境变量提供：

```bash
export ANTHROPIC_API_KEY='sk-ant-...'
```

> `history` / `status` / `doctor` 三个命令不调用 LLM，无需 API Key。

## 2. 准备数据库与凭证

pg-agent 通过**环境变量中的连接串**连接数据库（出于安全考虑不写入配置文件）：

```bash
export PGAGENT_DATABASE_URL='postgresql://user:password@host:5432/dbname'
```

变量名可通过配置 `database.url_env` 修改（见 §7）。建议为 agent 单独建一个角色与库：

```sql
CREATE ROLE pgagent LOGIN PASSWORD '强密码';
CREATE DATABASE mydb OWNER pgagent;
```

agent 需要的权限：目标 schema 的 `USAGE`/`CREATE`、对用户表的 `SELECT`（内省）、以及执行 DDL 的权限。它会自建并维护台账表 `agent_schema_migrations`。

> ⚠️ 请勿将 agent 指向使用超级用户角色的生产库——它能执行的 DDL 上限即该角色的权限上限。

## 3. 快速上手

```bash
# ① 体检：配置、连通性、内省权限、台账
pgagent doctor

# ② 进入交互会话
pgagent chat
```

会话内直接说需求：

```
你 › 帮我设计一个博客的核心表：文章表，支持标题、正文、作者、创建时间；
     常见查询是按作者找最新文章。
```

agent 会依次：`list_tables` 了解现状 → `inspect_schema` 确认相关表结构 → 生成 DDL → `preview_sql` 在事务中试执行（失败则根据数据库真实报错自动修正，最多重试 5 次）→ 向你展示 SQL 与风险评级请求确认 → 应用为迁移文件并写台账 → 报告结果。

离开会话用 `/exit` 或 Ctrl+D。之后随时：

```bash
pgagent status    # 对账 + 漂移检测
pgagent history   # 迁移历史
```

## 4. CLI 命令参考

所有命令支持 `--config/-c PATH` 指定配置文件路径（默认自动查找，见 §7）。

### `pgagent chat`

交互式会话，自然语言设计与迭代表结构。

| 选项 | 说明 |
|---|---|
| `--no-stream` | 关闭流式输出（默认流式） |
| `-v / --verbose` | 输出调试日志 |

启动时自动：加载配置 → 建库连接 → 创建台账表与目录结构（幂等）→ 加载 `SCHEMA_DESIGN.md` 作为设计上下文 → 新建会话文件。数据库连不上会直接报错退出。

### `pgagent history`

以表格展示迁移历史台账：版本、名称、需求摘要、耗时、应用时间。只读。

### `pgagent status`

对账与漂移检测（运维首查命令）。检查三类不一致：

- 存在迁移文件但台账无记录（未应用或中途失败）
- 台账有记录但迁移文件缺失
- 迁移文件与台账 checksum 不一致（文件被手改）

外加漂移检测：对比当前数据库结构指纹与上次 agent 应用后保存的基线（`.pgagent/state.json`），不一致则告警"可能有人绕过 pg-agent 手工改库"。全部正常时输出 `✅ 一致`。

### `pgagent doctor`

不落任何变更的体检：配置可加载 → 数据库连通（打印服务端版本）→ 内省权限正常 → 台账表就绪。接入新环境先跑它。

## 5. 会话内交互

### 内置命令

| 命令 | 作用 |
|---|---|
| `/status` | 同 `pgagent status`（对账 + 漂移） |
| `/history` | 同 `pgagent history` |
| `/schema` | 当前表清单（列数、行数估计、占用空间） |
| `/help` | 命令帮助 |
| `/exit`、`/quit`、Ctrl+D | 退出会话 |

### agent 的工作方式

每轮对话中 agent 只能调用 5 个受控工具，没有"执行任意 SQL"的通道：

| 工具 | 作用 |
|---|---|
| `list_tables` | 列出用户表（含行数估计，用于风险判断） |
| `inspect_schema` | 读取指定表的精确 DDL（列/约束/索引） |
| `preview_sql` | 在事务中试执行 SQL 后**回滚**，由真实数据库验证 |
| `apply_migration` | 唯一的写入入口：落盘迁移文件、事务内执行并写台账 |
| `list_migrations` | 查看已应用的迁移历史 |

试执行失败时数据库的真实错误（如 `42P01 关系 "xxx" 不存在`）会回传给模型供其自愈；连续失败达到 `llm.max_preview_iterations`（默认 5）本轮终止，避免无限烧 token。

### 确认门控

中/高风险迁移会弹出确认面板：风险评级、命中规则、需求摘要、带行号的 SQL。三个选择：

- **y** — 应用
- **n** — 拒绝（可填拒绝原因，原样反馈给 agent 供其调整方案）
- **e** — 在 `$EDITOR`（默认 vi）中手工编辑 SQL 后应用编辑版（文件与台账均记录编辑后内容）

拒绝**不留任何痕迹**：不写文件、不写台账、不改数据库。Ctrl+C 可随时中断当前轮次，已应用的迁移不受影响。

### 会话记录

每次会话自动保存为 `.pgagent/sessions/<会话ID>.jsonl`（完整消息流），可事后审计 agent 的每一步。

## 6. 风险分级与确认策略

每条语句经静态规则评级，取**最高级**作为迁移整体风险。策略由 `safety.allow_destructive` 控制：

| 策略 | 高危 | 中危 | 低危 |
|---|---|---|---|
| `ask`（默认） | 必须人工确认 | 必须人工确认 | 自动应用 |
| `deny` | **直接拒绝** | 直接拒绝 | 自动应用 |
| `allow` | 必须人工确认 | 自动应用 | 自动应用 |

高危始终需要确认（`deny` 除外），这是不可放宽的底线。评级规则：

| 级别 | 触发 |
|---|---|
| 🔴 高危 | `DROP TABLE`、`TRUNCATE`、`DROP COLUMN`、`DROP CONSTRAINT`、`ALTER` 修改列类型、任何 `CASCADE` |
| 🟡 中危 | `SET NOT NULL`（可能被既有 NULL 行违反）、新增 NOT NULL 无默认值列、新增外键、普通 `CREATE INDEX`（持锁阻塞写入）、`RENAME`、**大表上的任何 ALTER**（估计行数 > `large_table_rows`）、其他无法归类的 ALTER 形态 |
| 🟢 低危 | 新增可空列、`CREATE INDEX CONCURRENTLY`、创建新对象（表/视图/函数等）、`COMMENT` |

典型例子：对百万行大表加列是中危（走确认），新建空表加列是低危（直接应用）。

## 7. 配置参考（pgagent.toml）

启动时按以下顺序查找配置：`--config` 指定路径 → 从当前目录**向上**逐级查找 `pgagent.toml` → 都找不到则全默认（项目根 = cwd）。找不到配置文件不是错误。

```toml
[database]
url_env = "PGAGENT_DATABASE_URL"  # 连接串所在环境变量名
lock_timeout = "5s"               # DDL 等锁超时（快速失败，防锁队列放大）
statement_timeout = "60s"         # 语句超时

[llm]
model = "claude-opus-5"
effort = "high"                   # low / medium / high / xhigh / max
max_tokens = 16000
max_preview_iterations = 5        # 试执行失败自动重试上限

[safety]
allow_destructive = "ask"         # ask / deny / allow
large_table_rows = 100000         # 超过此估计行数视为“大表”（中危）
statement_whitelist = ["CREATE", "ALTER", "DROP", "COMMENT", "TRUNCATE"]

[storage]
migrations_dir = "migrations"     # 迁移文件目录
design_log = "SCHEMA_DESIGN.md"   # 设计日志
state_dir = ".pgagent"            # 会话与状态目录
```

说明：

- 超时格式：`数字 + ms|s|min`（如 `500ms`、`5s`、`1min`），非法值启动即报错。
- **白名单是 L1 硬闸**：不在名单内的语句首词（`SELECT`/`INSERT`/`UPDATE`/`DELETE`/`GRANT`/`COPY`/`SET`…）直接拒绝，不进数据库。一般无需修改；如确需收窄（如禁 `TRUNCATE`），删词条即可。所有路径相对**配置文件所在目录**解析。

## 8. 产出物与目录结构

一次成功的 apply 之后：

```
项目根/
├── migrations/
│   ├── 20260917152223_create-blog-core.sql        # 主迁移
│   └── 20260917152223_create-blog-core.down.sql   # 回滚迁移（仅当提供 down_sql）
├── SCHEMA_DESIGN.md                               # 设计日志（每次迁移追加一节）
└── .pgagent/
    ├── state.json                                 # 结构指纹基线（漂移检测用）
    └── sessions/20260917-152218-1234.jsonl        # 会话消息流
```

- **迁移文件**：`版本_名称.sql`，版本为 UTC 时间戳 `YYYYMMDDHHMMSS`（同秒冲突自动加后缀），名称 kebab-case。文件带注释头（名称/需求摘要），正文是纯 SQL，**不含** BEGIN/COMMIT——事务由 runner 包裹，文件本身可被任何标准工具重放。
- **台账表** `agent_schema_migrations`：版本、名称、checksum（sha256）、完整 SQL、需求摘要、会话 ID、耗时、down_sql 及其验证结果、应用时间。DDL 与台账写入在**同一事务**内提交，不会出现"库改了但没记录"。该表对内省不可见，不会污染 agent 的表清单。
- **down migration**：主迁移应用**成功后**才试执行验证（此时被回滚的对象才存在），结果记入台账 `down_valid`；验证失败仅告警，不阻断主迁移。注意 pg-agent 目前**记录并验证**回滚方案，不提供一键回滚命令（见 §11）。
- **SCHEMA_DESIGN.md**：设计日志，头注即规则——可人工修订，修订后 agent 在新会话中采用人工版本（作为设计上下文注入系统提示，默认截取前 6000 字符）。

## 9. 典型工作流

### 9.1 全新库从零设计

```
你 › 这是一个全新库。设计电商订单域： customers、orders、order_items 三张表，
     orders 需要冗余 customer 快照字段（下单时姓名），金额用 numeric。
```

agent 会先 `list_tables` 确认是空库，然后分批设计（通常一个迁移建一组强相关表），每个迁移走 preview → 确认 → 应用。

### 9.2 迭代变更（第二天继续）

新会话自动从 `SCHEMA_DESIGN.md` + 实时内省恢复上下文，直接说增量需求：

```
你 › 给 orders 加一个 status 字段，枚举：pending/paid/shipped/cancelled，
     默认 pending，并给 status + created_at 建组合索引。
```

agent 会 `inspect_schema` 确认 orders 真实结构后再改；枚举列会选 `TEXT + CHECK` 或 `CREATE TYPE` 并说明权衡。

### 9.3 破坏性变更（删表/删列/改类型）

高危变更必过确认门控。建议在需求里同时要回滚方案：

```
你 › 订单已经不存物理删除的临时表 tmp_export 了，删掉它。
     提供 down migration。
```

确认面板中可用 **e** 手工微调 SQL 再应用。

### 9.4 处理漂移

有人绕过 agent 手工改了库后，`pgagent status` 会告警漂移。处理方式：新会话中让 agent `inspect_schema` 核实现状，然后：

- 手工改动合理 → 让 agent 把现状固化成一个迁移说明（或手工把 `.pgagent/state.json` 中 `schema_fingerprint` 更新为当前值——可用任意一次成功 apply 后的 state.json 结构参考，或直接删掉 state.json 让下次 apply 重建基线）；
- 手工改动错误 → 让 agent 生成反向迁移修正，应用后基线自动恢复一致。

### 9.5 只读查询场景

会话中问"现在有哪些表？orders 的结构是什么？"——agent 只会调用内省类工具，不会产生任何变更。

## 10. 安全模型

- **能力面收敛**：模型只有 5 个受控工具，无任意 SQL 通道；`apply_migration` 的 SQL 在 runner 内还会被防御性二次校验（即使工具层被绕过，DML 依旧在 L1 被拒）。
- **四层校验**：L0 pglast 语法解析 → L1 语句白名单 → L2 风险规则 → L3 真实事务试执行（L4 影子库为规划中）。
- **DDL 锁保护**：所有事务自动 `SET LOCAL lock_timeout / statement_timeout`，拿不到锁快速失败，避免 DDL 锁队列拖垮业务查询。
- **不可事务化的语句**（如 `CREATE INDEX CONCURRENTLY`）被单独识别：preview 时跳过并警告；apply 时走非事务路径逐条执行、事后写台账（此路径下失败不自动回滚已执行语句，apply 失败信息会如实报告）。
- **审计闭环**：谁（会话 ID）、为什么（需求摘要）、改了什么（完整 SQL + checksum）、何时（时间戳）全部可查。

## 11. 故障排查

| 现象 | 原因与处理 |
|---|---|
| `环境变量 PGAGENT_DATABASE_URL 未设置` | 设置连接串环境变量；变量名若改过配置，导出对应名字 |
| `pgagent doctor` 连接失败 | 检查连接串/网络/pg_hba 认证方式；密码认证推荐 scram-sha-256 |
| chat 报 API 认证错误 | 未设置 `ANTHROPIC_API_KEY` 或 key 无效；或改用不需要 LLM 的 `history/status` |
| apply 报 `42P01 台账不存在` | 台账表被手工删除（如 DROP SCHEMA）。跑一次 `pgagent doctor` 会自动重建，或任意 `chat` 启动时自动创建 |
| `status`：存在迁移文件但台账无记录 | 上次 apply 中途失败（文件已写、事务未提交）。核实数据库现状后：重试该变更（用新文件），或删除孤儿文件 |
| `status`：checksum 不一致 | 迁移文件被手改。以数据库实际结构为准：接受手工版则同时更新台账（或删除文件+记录重做）；否则还原文件 |
| `status`：漂移告警 | 有人绕过 agent 手工改库，见 §9.4 |
| preview 反复失败后本轮终止 | 达到 `max_preview_iterations` 上限（默认 5）。看 agent 最后收到的数据库错误，人工介入调整需求或 SQL 后重开一轮 |
| 高危迁移想全自动 | 不建议。确有需要时对中危可设 `allow_destructive = "allow"`；高危无论如何都会确认 |
| 想撤销已应用的迁移 | 手工执行对应 `.down.sql`（先确认其台账 `down_valid` 为真），并手工对账台账/文件/state 基线；一键回滚命令在路线图中（设计文档 §4.10） |
| 会话中 Ctrl+C 之后 | 仅中断当前轮；已应用迁移不受影响，会话文件保留到中断前的最后一条完整消息 |

## 12. 运行测试

测试需要真实 PostgreSQL（集成测试覆盖事务回滚、门控、台账原子性等）：

```bash
# 准备测试库（默认连接串如下，可用环境变量覆盖）
psql -c "CREATE ROLE pgagent LOGIN PASSWORD 'pgagent_pass';"
psql -c "CREATE DATABASE pgagent_test OWNER pgagent;"
export PGAGENT_TEST_URL='postgresql://pgagent:pgagent_pass@127.0.0.1:5432/pgagent_test'

pip install -e ".[dev]"
python -m pytest tests/
```

单元测试（配置/解析/风险规则）不需要数据库，无库时集成测试自动跳过。
