"""共享异常类型。"""

from __future__ import annotations


class PgAgentError(Exception):
    """pg-agent 所有自定义异常的基类。"""


class SqlValidationError(PgAgentError):
    """验证管线 L0-L2 失败（语法/白名单/风险外的确定性拒绝）。

    layer: "L0-syntax" | "L1-whitelist" | "L2-risk"
    """

    def __init__(self, layer: str, message: str) -> None:
        super().__init__(f"[{layer}] {message}")
        self.layer = layer


class GateRejected(PgAgentError):
    """变更在确认门控处被拒绝（配置拒绝或用户拒绝）。"""


class RunnerError(PgAgentError):
    """迁移 runner 执行失败（文件/台账/状态不一致等）。"""


class ConfigError(PgAgentError):
    """配置缺失或非法。"""
