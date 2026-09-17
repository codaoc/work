"""配置单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from pgagent.config import Config, DatabaseConfig, load_config
from pgagent.errors import ConfigError


def test_defaults():
    cfg = Config()
    cfg.database.validate()
    cfg.llm.validate()
    cfg.safety.validate()
    assert cfg.llm.model == "claude-opus-5"
    assert cfg.safety.allow_destructive == "ask"


def test_url_env_missing_raises(monkeypatch):
    monkeypatch.delenv("PGAGENT_DATABASE_URL", raising=False)
    with pytest.raises(ConfigError):
        DatabaseConfig().resolve_url()


def test_timeout_validation():
    with pytest.raises(ConfigError):
        DatabaseConfig(lock_timeout="5seconds").validate()


def test_load_from_toml(tmp_path: Path):
    toml = tmp_path / "pgagent.toml"
    toml.write_text(
        """
[llm]
model = "claude-sonnet-5"
effort = "medium"

[safety]
allow_destructive = "deny"
large_table_rows = 5
""",
        encoding="utf-8",
    )
    cfg = load_config(toml)
    assert cfg.llm.model == "claude-sonnet-5"
    assert cfg.llm.effort == "medium"
    assert cfg.safety.allow_destructive == "deny"
    assert cfg.safety.large_table_rows == 5
    assert cfg.project_root == tmp_path
    # 相对路径基于配置文件目录解析
    assert cfg.migrations_dir == tmp_path / "migrations"


def test_invalid_destructive_policy(tmp_path: Path):
    toml = tmp_path / "pgagent.toml"
    toml.write_text('[safety]\nallow_destructive = "maybe"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(toml)
