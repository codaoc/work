#!/usr/bin/env bash
# ============================================================================
# pg-mcp 启动脚本
# ============================================================================
# 项目的嵌套配置类 (DatabaseConfig 等) 未配置 env_file，.env 文件不会被自动
# 读取 —— 只有真实环境变量生效。此脚本用 set -a 把 .env 导出为环境变量后再
# 启动服务器，等效于 README 中描述的 .env 行为。
#
# 用法: ./run.sh
# 前置: 在 .env 中取消注释并填写 OPENAI_API_KEY
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
    echo "错误: 找不到 .env，请先执行 cp .env.example .env 并配置" >&2
    exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
    echo "错误: OPENAI_API_KEY 未设置，请编辑 .env 取消注释并填入有效密钥" >&2
    exit 1
fi

exec uv run python -m pg_mcp
