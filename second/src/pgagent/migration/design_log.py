"""设计日志 SCHEMA_DESIGN.md（FR-8：跨会话记忆，设计文档 §4.8）。

agent 每次成功应用迁移后追加一节；内容随项目进 git，
新会话启动时摘要注入系统提示词，使 agent"认识"这个库的历史与决策。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

_HEADER = (
    "# Schema 设计日志\n\n"
    "> 本文件由 pg-agent 维护，记录每次结构变更及设计决策。可人工修订，"
    "修订后 agent 将在新会话中采用人工版本。\n"
)


def append_entry(
    path: Path,
    *,
    version: str,
    name: str,
    request: str,
    risk_label: str,
    tables_touched: list[str],
    sql: str,
    duration_ms: int,
) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    tables = ", ".join(f"`{t}`" for t in tables_touched) or "-"
    sql_block = "\n".join("    " + line for line in sql.strip().splitlines())
    section = (
        f"\n## {version} {name}\n\n"
        f"- 时间：{ts}\n- 风险：{risk_label}\n- 涉及表：{tables}\n"
        f"- 需求：{request}\n- 耗时：{duration_ms} ms\n\n"
        "```sql\n" + sql_block + "\n```\n"
    )
    if not path.exists() or path.read_text(encoding="utf-8").strip() == "":
        path.write_text(_HEADER + section, encoding="utf-8")
    else:
        with open(path, "a", encoding="utf-8") as f:
            f.write(section)


def read_context(path: Path, max_chars: int = 6000) -> str:
    """读取设计日志摘要注入系统提示词；过长时截取最近部分。"""
    if not path.exists():
        return "（设计日志为空：这很可能是一个全新数据库，尚无历史迁移。）"
    text = path.read_text(encoding="utf-8")
    if len(text) <= max_chars:
        return text
    head, _, body = text.partition("\n")
    return head + "\n\n（历史较长，已截取最近部分）\n\n…" + text[-max_chars:]
