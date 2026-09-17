"""配置加载（设计文档 §6 / pgagent.toml）。

存储类路径相对配置文件所在目录解析；数据库连接串只从环境变量读取。
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

_TIMEOUT_RE = re.compile(r"^\d+(\.\d+)?(ms|s|min)$")


@dataclass(frozen=True)
class DatabaseConfig:
    url_env: str = "PGAGENT_DATABASE_URL"
    lock_timeout: str = "5s"
    statement_timeout: str = "60s"

    def resolve_url(self) -> str:
        url = os.environ.get(self.url_env, "").strip()
        if not url:
            raise ConfigError(
                f"环境变量 {self.url_env} 未设置，无法连接数据库。"
                f"请设置形如 postgresql://user:pass@host:5432/dbname 的连接串。"
            )
        return url

    def validate(self) -> None:
        for name in ("lock_timeout", "statement_timeout"):
            v = getattr(self, name)
            if not _TIMEOUT_RE.match(v):
                raise ConfigError(f"database.{name}='{v}' 非法，应为如 5s / 500ms / 1min 的时长")


@dataclass(frozen=True)
class LLMConfig:
    model: str = "claude-opus-5"
    effort: str = "high"
    max_tokens: int = 16000
    max_preview_iterations: int = 5

    def validate(self) -> None:
        if self.effort not in ("low", "medium", "high", "xhigh", "max"):
            raise ConfigError(f"llm.effort='{self.effort}' 非法")
        if self.max_preview_iterations < 1:
            raise ConfigError("llm.max_preview_iterations 必须 >= 1")


@dataclass(frozen=True)
class SafetyConfig:
    allow_destructive: str = "ask"
    large_table_rows: int = 100_000
    statement_whitelist: tuple[str, ...] = ("CREATE", "ALTER", "DROP", "COMMENT", "TRUNCATE")

    def validate(self) -> None:
        if self.allow_destructive not in ("ask", "deny", "allow"):
            raise ConfigError("safety.allow_destructive 只能是 ask / deny / allow")


@dataclass(frozen=True)
class StorageConfig:
    migrations_dir: Path = field(default_factory=lambda: Path("migrations"))
    design_log: Path = field(default_factory=lambda: Path("SCHEMA_DESIGN.md"))
    state_dir: Path = field(default_factory=lambda: Path(".pgagent"))


@dataclass(frozen=True)
class Config:
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    project_root: Path = field(default_factory=Path.cwd)

    @property
    def migrations_dir(self) -> Path:
        return self.project_root / self.storage.migrations_dir

    @property
    def design_log_path(self) -> Path:
        return self.project_root / self.storage.design_log

    @property
    def state_dir(self) -> Path:
        return self.project_root / self.storage.state_dir


def _section(data: dict, name: str) -> dict:
    v = data.get(name, {})
    if not isinstance(v, dict):
        raise ConfigError(f"配置段 [{name}] 应为表")
    return v


def load_config(path: Path | None = None) -> Config:
    """从 TOML 加载配置；path 为 None 时从 cwd 向上查找 pgagent.toml，找不到则全默认。"""
    cfg_path = path
    if cfg_path is None:
        p = Path.cwd()
        for candidate in (p, *p.parents):
            if (candidate / "pgagent.toml").is_file():
                cfg_path = candidate / "pgagent.toml"
                break
        if cfg_path is None:
            cfg = Config(project_root=Path.cwd())
            _validate(cfg)
            return cfg

    cfg_path = cfg_path.resolve()
    with open(cfg_path, "rb") as f:
        data = tomllib.load(f)

    db = _section(data, "database")
    llm = _section(data, "llm")
    safety = _section(data, "safety")
    storage = _section(data, "storage")

    root = cfg_path.parent
    cfg = Config(
        database=DatabaseConfig(
            url_env=db.get("url_env", "PGAGENT_DATABASE_URL"),
            lock_timeout=db.get("lock_timeout", "5s"),
            statement_timeout=db.get("statement_timeout", "60s"),
        ),
        llm=LLMConfig(
            model=llm.get("model", "claude-opus-5"),
            effort=llm.get("effort", "high"),
            max_tokens=llm.get("max_tokens", 16000),
            max_preview_iterations=llm.get("max_preview_iterations", 5),
        ),
        safety=SafetyConfig(
            allow_destructive=safety.get("allow_destructive", "ask"),
            large_table_rows=safety.get("large_table_rows", 100_000),
            statement_whitelist=tuple(safety.get(
                "statement_whitelist", ("CREATE", "ALTER", "DROP", "COMMENT", "TRUNCATE")
            )),
        ),
        storage=StorageConfig(
            migrations_dir=Path(storage.get("migrations_dir", "migrations")),
            design_log=Path(storage.get("design_log", "SCHEMA_DESIGN.md")),
            state_dir=Path(storage.get("state_dir", ".pgagent")),
        ),
        project_root=root,
    )
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    cfg.database.validate()
    cfg.llm.validate()
    cfg.safety.validate()
