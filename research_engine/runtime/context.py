"""一次 run 的运行期上下文 + 节点 token 写回保证（2026-11）。"""
from __future__ import annotations

import functools
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

#: 取消谓词：返回 True 表示「当前节点结束后不要再启动下一节点」。
CancelPredicate = Callable[[], bool]


@dataclass
class RunContext:
    """一次 run 的身份与运行期句柄（**不跨 run 共享**）。

    只放「进程内、按 run 唯一」的东西；可序列化状态一律留在 ``ResearchState``。
    """

    run_id: str
    trace_id: Optional[str] = None
    cancel: Optional[CancelPredicate] = None
    started_at: float = field(default_factory=time.time)

    def cancelled(self) -> bool:
        """协作式取消判定；谓词自身抛错**向上传播**（不在这里静默吞掉，避免掩盖坏谓词）。"""
        if self.cancel is None:
            return False
        return bool(self.cancel())

    def elapsed_s(self) -> float:
        return round(time.time() - self.started_at, 3)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "elapsed_s": self.elapsed_s(),
        }


def new_run_context(
    thread_id: Optional[str] = None,
    cancel: Optional[CancelPredicate] = None,
    trace_id: Optional[str] = None,
) -> RunContext:
    """新建一次 run 的上下文（run_id 缺省时生成 ``dr-<hex12>``）。"""
    return RunContext(
        run_id=thread_id or f"dr-{uuid.uuid4().hex[:12]}",
        trace_id=trace_id,
        cancel=cancel,
    )


def with_token_writeback(fn: Callable[..., Dict[str, Any]]) -> Callable[..., Dict[str, Any]]:
    """节点装饰器：保证 delta 里**一定**带 ``token_used``。

    契约：被装饰的节点签名为 ``(self, state) -> dict``。节点内部调用 LLM 时，
    用量已经由 ``budget`` 原子写进 ``state.token_used``；本装饰器只负责把当前值
    随 delta 带回 LangGraph 通道，避免「某个节点忘了写回 ⇒ 预算闸看不到」的类问题。
    """

    @functools.wraps(fn)
    def _wrapped(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        delta = fn(*args, **kwargs)
        if isinstance(delta, dict) and "token_used" not in delta:
            state = args[1] if len(args) > 1 else kwargs.get("state")
            if state is not None:
                delta["token_used"] = getattr(state, "token_used", 0)
        return delta

    _wrapped.__token_writeback__ = True  # type: ignore[attr-defined]
    return _wrapped
