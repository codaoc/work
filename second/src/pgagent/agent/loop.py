"""Agent 循环（设计文档 §4.1）。

采用手动循环而非 SDK Tool Runner：需要循环级控制——试执行自愈上限、
确认门控中断、CLI 流式渲染、每轮 token 用量审计。
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anthropic
from rich.console import Console

from ..config import Config
from ..logging import get_logger
from .prompts import system_blocks
from .tools import TOOL_DEFS, ToolContext, dispatch

log = get_logger("agent.loop")


@dataclass
class Session:
    """会话状态：消息历史持久化为 JSONL（审计 + 可恢复的基础）。"""

    id: str
    path: Path
    messages: list[dict[str, Any]]

    @classmethod
    def create(cls, state_dir: Path) -> "Session":
        state_dir.mkdir(parents=True, exist_ok=True)
        sid = time.strftime("%Y%m%d-%H%M%S") + f"-{random.randint(1000, 9999)}"
        return cls(id=sid, path=state_dir / "sessions" / f"{sid}.jsonl", messages=[])

    def append(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(message, ensure_ascii=False, default=_json_default) + "\n")


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):  # anthropic 内容块是 pydantic 模型
        return obj.model_dump()
    return str(obj)


class AgentLoop:
    def __init__(
        self,
        client: anthropic.Anthropic,
        cfg: Config,
        ctx: ToolContext,
        session: Session,
        console: Console,
        system_prompt: str,
        stream: bool = True,
    ) -> None:
        self.client = client
        self.cfg = cfg
        self.ctx = ctx
        self.session = session
        self.console = console
        self.system = system_blocks(system_prompt)
        self.stream = stream

    def run_turn(self, user_input: str) -> str:
        """处理一条用户输入，驱动工具循环直到模型给出最终答复。"""
        self.ctx.introspected = False
        self.ctx.preview_failures = 0
        self.session.append({"role": "user", "content": user_input})

        while True:
            response = self._request()

            self.session.append({"role": "assistant", "content": response.content})
            self._log_usage(response)

            if response.stop_reason == "end_turn":
                return self._text(response)
            if response.stop_reason == "refusal":
                return self._text(response) + "\n（模型拒绝了该请求，请调整需求后重试。）"
            if response.stop_reason == "max_tokens":
                return self._text(response) + "\n（回复因长度上限被截断，请让模型继续或简化需求。）"
            # tool_use / pause_turn：继续循环

            calls = [b for b in response.content if b.type == "tool_use"]
            results = []
            for call in calls:
                self.console.print(f"  [dim]⚙ {call.name}[/dim]")
                result = dispatch(self.ctx, call.name, dict(call.input))
                results.append({
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": result.content,
                    **({"is_error": True} if result.is_error else {}),
                })

            # 协议要求：所有 tool_use 必须有对应 tool_result 才能继续请求
            self.session.append({"role": "user", "content": results})

            if self.ctx.preview_failures >= self.cfg.llm.max_preview_iterations:
                note = (
                    f"preview_sql 已连续失败 {self.ctx.preview_failures} 次，"
                    "为避免无限重试已终止本轮。请人工检查 SQL 或调整需求。"
                )
                self.console.print(f"  [red]{note}[/red]")
                return note

    # ------------------------------------------------------------------

    def _request(self):
        kwargs = dict(
            model=self.cfg.llm.model,
            max_tokens=self.cfg.llm.max_tokens,
            system=self.system,
            thinking={"type": "adaptive"},
            output_config={"effort": self.cfg.llm.effort},
            tools=TOOL_DEFS,
            messages=self.session.messages,
        )
        if self.stream:
            with self.client.messages.stream(**kwargs) as stream:
                for chunk in stream.text_stream:
                    self.console.print(chunk, end="")
                self.console.print()
                return stream.get_final_message()
        return self.client.messages.create(**kwargs)

    def _log_usage(self, response) -> None:
        u = response.usage
        log.info(
            "LLM 用量: in=%s out=%s cache_read=%s cache_write=%s stop=%s",
            u.input_tokens, u.output_tokens,
            getattr(u, "cache_read_input_tokens", 0) or 0,
            getattr(u, "cache_creation_input_tokens", 0) or 0,
            response.stop_reason,
        )

    @staticmethod
    def _text(response) -> str:
        return "\n".join(b.text for b in response.content if b.type == "text")


def make_client() -> anthropic.Anthropic:
    """构造 SDK 客户端；鉴权错误在首次请求时才发生，这里只做构造。"""
    return anthropic.Anthropic()


__all__ = ["AgentLoop", "Session", "make_client"]
