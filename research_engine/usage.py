"""逐调用 usage 记账的运行时 sink（P1-4 / P0-8）。

核心链路（LLMClient / embedding / 搜索）只负责 `emit_usage`；**落库由 web 层注入**。
2026-11 起：研究主链路按 `run_id` 走**显式注册表 sink**（`register_usage_sink`），
contextvar sink 仅作文档摄取 / MCP server 等非研究 run 的兼容兜底：

- CLI / eval 不注册 sink ⇒ emit 为 no-op，行为与历史一致；
- RunManager / Worker 在每次 run 前 `register_usage_sink(run_id, ...)`、结束注销；
  同时保留 `push_usage_sink` 兼容路径（尚未携带 run_id 的 emit 点）；
- **P0-8：sink 异常不再静默**——记 ERROR 日志并累加 `sink_failure_count()`；
  `DR_USAGE_STRICT=true` 时上抛 `UsageSinkError`（预算/计费场景可将任务标记为
  accounting_degraded 或中止）。web 层 sink 还会写审计与 Prometheus 计数（见
  `web/backend/usage.py`）。
"""
from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

_log = logging.getLogger("deepresearch.usage")

#: 业务关键用量落账失败（`DR_USAGE_STRICT=true` 时由 emit_usage 上抛）
class UsageSinkError(RuntimeError):
    pass


_sink_failures = 0


def sink_failure_count() -> int:
    """进程内 sink 失败累计（监控/测试用）。"""
    return _sink_failures


def _strict() -> bool:
    return (os.getenv("DR_USAGE_STRICT") or "false").strip().lower() in {
        "1", "true", "yes", "on"}


@dataclass(frozen=True)
class UsageRecord:
    """一次外部调用的用量事实（不含 prompt / PII）。"""

    kind: str                          # llm | embedding | search
    provider: str
    model: str = ""
    role: str = ""                     # llm 职责桶（planner/critic/...）；search 工具名
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    request_id: Optional[str] = None   # provider 响应 id（对账 / 去重用）


_sink: ContextVar[Optional[Callable[[UsageRecord], None]]] = ContextVar(
    "dr_usage_sink", default=None)

#: 2026-11：按 run_id 注册的显式 sink（主链路迁移目标；contextvar 仅作旧调用/单测兜底）。
#: emit 点携带 run_id 时优先走这里 —— 不再依赖「执行器先 push 再调用」的线程上下文。
_sinks: dict[str, Callable[[UsageRecord], None]] = {}
_sinks_lock = threading.Lock()


def register_usage_sink(run_id: str, sink: Callable[[UsageRecord], None]) -> None:
    """按 run_id 注册逐调用记账 sink（run 开始调用；与 :func:`unregister_usage_sink` 配对）。"""
    if not run_id:
        raise ValueError("register_usage_sink 需要非空 run_id")
    with _sinks_lock:
        _sinks[run_id] = sink


def unregister_usage_sink(run_id: str) -> None:
    """注销 run 的 sink（run 结束调用；重复注销安全）。"""
    with _sinks_lock:
        _sinks.pop(run_id, None)


def usage_sink_for(run_id: str) -> Optional[Callable[[UsageRecord], None]]:
    """按 run_id 取显式 sink（未注册/空 id ⇒ None）。"""
    if not run_id:
        return None
    with _sinks_lock:
        return _sinks.get(run_id)


def _deliver(sink: Callable[[UsageRecord], None], record: UsageRecord) -> None:
    """把记录交给一个 sink；失败计数 + ERROR 日志，strict 模式上抛（P0-8）。"""
    global _sink_failures
    try:
        sink(record)
    except Exception as exc:  # noqa: BLE001 —— 记账旁路：默认不打断主链路，但不许静默
        _sink_failures += 1
        _log.error("usage sink failed (kind=%s provider=%s request_id=%s): %s: %s",
                   record.kind, record.provider, record.request_id,
                   type(exc).__name__, exc)
        if _strict():
            raise UsageSinkError(f"usage sink failed: {type(exc).__name__}: {exc}") from exc


def emit_usage(record: UsageRecord, run_id: Optional[str] = None) -> None:
    """发送一条用量：显式 ``run_id`` 优先走注册表，否则回退 contextvar sink；都没有则 no-op。"""
    sink = usage_sink_for(run_id or "")
    if sink is None:
        sink = _sink.get()
    if sink is None:
        return
    _deliver(sink, record)


def push_usage_sink(sink: Callable[[UsageRecord], None]) -> object:
    """设置当前 sink，返回 token（配合 :func:`pop_usage_sink` 复位）。"""
    return _sink.set(sink)


def pop_usage_sink(token: object) -> None:
    _sink.reset(token)  # type: ignore[arg-type]


@contextmanager
def use_usage_sink(sink: Callable[[UsageRecord], None]) -> Iterator[None]:
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)
