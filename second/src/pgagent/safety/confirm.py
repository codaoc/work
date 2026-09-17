"""确认门控（设计文档 §4.6 / §4.9）。

高危变更的最终裁决权在人类；allow_destructive=deny 时高危直接拒绝。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Protocol

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.syntax import Syntax

from ..config import SafetyConfig
from ..errors import PgAgentError
from .risk import RiskAssessment, RiskLevel


@dataclass(frozen=True)
class Confirmation:
    approved: bool
    sql: str          # 用户可能手工编辑后的 SQL
    note: str = ""    # 拒绝原因等备注


class ConfirmGate(Protocol):
    def confirm(self, assessment: RiskAssessment, sql: str, name: str, request: str) -> Confirmation: ...


def gate_decision(assessment: RiskAssessment, cfg: SafetyConfig) -> str:
    """返回 "skip"（无需确认）/ "required"（需确认）/ "denied"（配置直接拒绝）。

    高危变更始终需要确认（设计文档 §9）；中危按 allow_destructive 策略；
    低危始终直接应用。
    """
    policy = cfg.allow_destructive
    if assessment.level >= RiskLevel.HIGH:
        return "denied" if policy == "deny" else "required"
    if assessment.level == RiskLevel.MEDIUM:
        return {"ask": "required", "deny": "denied", "allow": "skip"}[policy]
    return "skip"


class CliConfirmGate:
    """终端交互门控：渲染 SQL 与命中规则，等待用户裁决，支持 e 编辑。"""

    def __init__(self, console: Console) -> None:
        self.console = console

    def confirm(self, assessment: RiskAssessment, sql: str, name: str, request: str) -> Confirmation:
        self.console.print(Panel(
            f"迁移 [bold]{name}[/bold]（风险：{assessment.label}）\n"
            f"需求：{request}\n\n{assessment.describe()}",
            title="⚠️  需要人工确认", border_style="yellow",
        ))
        self.console.print(Syntax(sql, "sql", theme="ansi_dark", line_numbers=True))
        choice = Prompt.ask(
            "应用该迁移？[y]应用 / [n]拒绝 / [e]编辑 SQL",
            choices=["y", "n", "e"], default="n", console=self.console,
        )
        if choice == "y":
            return Confirmation(approved=True, sql=sql)
        if choice == "e":
            edited = _edit_in_editor(sql)
            if edited.strip() and edited != sql:
                self.console.print("[yellow]注意：SQL 已被手工编辑，将以编辑后的内容应用[/yellow]")
                return Confirmation(approved=True, sql=edited)
            return Confirmation(approved=True, sql=sql)
        reason = Prompt.ask("拒绝原因（将反馈给智能体，可留空）", default="", console=self.console)
        return Confirmation(approved=False, sql=sql, note=reason)


class NonInteractiveGate:
    """非交互环境的确定性门控（测试/脚本用）。

    approved=True 时全部批准；False 时按级别批准低危、拒绝其余。
    """

    def __init__(self, approved: bool, note: str = "非交互模式") -> None:
        self.approved = approved
        self.note = note

    def confirm(self, assessment: RiskAssessment, sql: str, name: str, request: str) -> Confirmation:
        if self.approved:
            return Confirmation(approved=True, sql=sql)
        if assessment.level < RiskLevel.MEDIUM:
            return Confirmation(approved=True, sql=sql)
        return Confirmation(approved=False, sql=sql, note=self.note)


class EditFailed(PgAgentError):
    pass


def _edit_in_editor(sql: str) -> str:
    editor = os.environ.get("EDITOR", "vi")
    with tempfile.NamedTemporaryFile("w+", suffix=".sql", delete=False) as f:
        f.write(sql)
        path = f.name
    try:
        subprocess.run([editor, path], check=True)
        with open(path) as f:
            return f.read()
    except subprocess.CalledProcessError as e:
        raise EditFailed(f"编辑器退出码 {e.returncode}，保留原 SQL") from None
    finally:
        os.unlink(path)
