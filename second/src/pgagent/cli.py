"""CLI 入口（typer）：pgagent chat / history / status / doctor。"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .agent.loop import AgentLoop, Session, make_client
from .agent.prompts import build_system_prompt
from .agent.tools import ToolContext, dispatch
from .config import Config, load_config
from .db.executor import Executor
from .db.introspect import list_tables
from .db.session import Database
from .logging import setup_logging
from .migration import design_log
from .migration.ledger import ensure_ledger
from .migration.runner import MigrationRunner
from .safety.confirm import CliConfirmGate

app = typer.Typer(help="pg-agent：自然语言驱动的 PostgreSQL 表结构设计与迁移智能体")
console = Console()


def _load_config(path: Path | None, verbose: bool = False) -> Config:
    setup_logging(verbose=verbose)
    return load_config(path)


def _build(cfg: Config, session_id: str) -> tuple[Database, MigrationRunner, ToolContext]:
    gate = CliConfirmGate(console)
    db = Database(
        cfg.database.resolve_url(),
        lock_timeout=cfg.database.lock_timeout,
        statement_timeout=cfg.database.statement_timeout,
    )
    executor = Executor(db)
    runner = MigrationRunner(cfg, db, executor, gate=gate, session_id=session_id)
    ctx = ToolContext(cfg=cfg, db=db, executor=executor, runner=runner,
                      console=console, session_id=session_id)
    return db, runner, ctx


@app.command()
def chat(
    config: Path | None = typer.Option(None, "--config", "-c", help="配置文件路径"),
    no_stream: bool = typer.Option(False, "--no-stream", help="关闭流式输出"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="调试日志"),
) -> None:
    """交互式会话：自然语言设计表结构与生成迁移。"""
    cfg = _load_config(config, verbose)
    session = Session.create(cfg.state_dir)
    db, runner, ctx = _build(cfg, session.id)

    try:
        runner.ensure_ready()
    except Exception as e:
        console.print(f"[red]数据库连接失败：{e}[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]pg-agent[/bold] 会话 [dim]{session.id}[/dim]")
    console.print("输入需求开始设计；内置命令：/status /history /schema /help /exit\n")

    client = make_client()
    prompt = build_system_prompt(design_log.read_context(cfg.design_log_path))
    loop = AgentLoop(client, cfg, ctx, session, console, prompt, stream=not no_stream)

    while True:
        try:
            user_input = console.input("[bold cyan]你 ›[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n再见。")
            break
        if not user_input:
            continue
        if user_input in ("/exit", "/quit"):
            console.print("再见。")
            break
        if user_input == "/status":
            _print_status(runner)
            continue
        if user_input == "/history":
            _print_history(runner)
            continue
        if user_input == "/schema":
            console.print(dispatch(ctx, "list_tables", {}).content)
            continue
        if user_input == "/help":
            console.print("内置命令：/status（对账+漂移） /history（迁移历史） /schema（表清单） /exit")
            continue

        try:
            reply = loop.run_turn(user_input)
        except KeyboardInterrupt:
            console.print("\n[yellow]已中断本轮（已应用的迁移不受影响）。[/yellow]")
            continue
        console.print(f"[bold green]agent ›[/bold green] {reply}\n")

    db.close()


@app.command()
def history(
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    """查看迁移历史台账。"""
    cfg = _load_config(config)
    db, runner, _ = _build(cfg, "cli")
    try:
        _print_history(runner)
    finally:
        db.close()


@app.command()
def status(
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    """对账迁移文件与台账，并检测手工改动导致的漂移。"""
    cfg = _load_config(config)
    db, runner, _ = _build(cfg, "cli")
    try:
        _print_status(runner)
    finally:
        db.close()


@app.command()
def doctor(
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    """体检：配置、数据库连通性、内省权限、台账。"""
    try:
        cfg = _load_config(config)
        console.print("✅ 配置加载成功")
    except Exception as e:
        console.print(f"❌ 配置加载失败：{e}")
        raise typer.Exit(1)

    db = Database(
        cfg.database.resolve_url(),
        lock_timeout=cfg.database.lock_timeout,
        statement_timeout=cfg.database.statement_timeout,
    )
    try:
        version = db.ping()
        console.print(f"✅ 数据库连通：{version.split(',')[0]}")
        console.print(f"✅ 内省权限正常（发现 {len(list_tables(db.conn))} 张用户表）")
        with db.transaction() as conn:
            ensure_ledger(conn)
        console.print("✅ 台账表就绪")
    except Exception as e:
        console.print(f"❌ 数据库问题：{e}")
        raise typer.Exit(1)
    finally:
        db.close()


def _print_status(runner: MigrationRunner) -> None:
    report = runner.status()
    issues = report["issues"]
    if not issues:
        console.print(f"[green]✅ 一致[/green]：{report['applied']} 条已应用，"
                      f"{report['files']} 个迁移文件，无漂移。")
    else:
        console.print(f"[yellow]⚠️ 发现 {len(issues)} 个问题：[/yellow]")
        for i in issues:
            console.print(f"  • {i}")


def _print_history(runner: MigrationRunner) -> None:
    rows = runner.history()
    table = Table(title="迁移历史")
    table.add_column("版本")
    table.add_column("名称")
    table.add_column("需求")
    table.add_column("耗时(ms)")
    table.add_column("应用时间")
    for r in rows:
        table.add_row(str(r["version"]), str(r["name"]), str(r["request"] or ""),
                      str(r["duration_ms"] or "?"), str(r["applied_at"]))
    console.print(table)


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
