"""一次 run 的 token 账本（2026-11：替代「类级全局计数器」的运行期依赖）。

为什么
------
旧实现用 ``LLMClient`` 的**类级**属性累计 token（跨 run、跨线程全局）。它有两个问题：
1. 并发 run 互相污染（eval 线程池 / 队列 Worker 多线程）；
2. 引擎运行期依赖一个全局可变对象做「本次 run 用了多少」的判断，语义不清晰。

现在：每次 run 的 ``RunContext`` 持有自己的 :class:`TokenLedger`，并以 ``run_id``
登记进进程级注册表；``ResearchState.run_id`` 随 LangGraph checkpoint 序列化，
LLM 记账时按 run_id 找到本 run 的账本写入。运行期统计以账本为准；
类级计数器仅保留给评测工具做跨 run 汇总（后续再迁）。

边界
----
- 只统计**传了 state 且 state 带 run_id** 的 LLM 调用（与预算 ``state.token_used`` 同口径）；
- 线程安全（validator 分批并行会并发记账）；
- 跨进程恢复的 run 在新进程注册表里没有账本 ⇒ 从恢复点重新累计（预期行为；
  成本权威仍以 ``usage_ledger`` / ``state.token_used`` 为准）。
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Optional

#: run_id → 本进程内活跃账本（run 结束即注销，防泄漏）
_ACTIVE: Dict[str, TokenLedger] = {}
_REGISTRY_LOCK = threading.Lock()


class TokenLedger:
    """run 级 token 账本（按 model / role 双轨，含 input/output 拆分）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total = 0
        self._by_model: Dict[str, int] = {}
        self._by_role: Dict[str, int] = {}
        self._by_model_io: Dict[str, Dict[str, int]] = {}

    def add(self, *, model: str, role: str, input_tokens: int = 0,
            output_tokens: int = 0, total_tokens: int | None = None) -> None:
        total = int(total_tokens if total_tokens is not None
                    else int(input_tokens) + int(output_tokens))
        with self._lock:
            self._total += total
            self._by_model[model] = self._by_model.get(model, 0) + total
            self._by_role[role] = self._by_role.get(role, 0) + total
            io = self._by_model_io.setdefault(model, {"input": 0, "output": 0})
            io["input"] += int(input_tokens)
            io["output"] += int(output_tokens)

    @property
    def total(self) -> int:
        with self._lock:
            return self._total

    def snapshot(self) -> Dict[str, Any]:
        """当前账本的不可变快照（可落盘 / 进报告）。"""
        with self._lock:
            return {
                "total": self._total,
                "by_model": dict(self._by_model),
                "by_role": dict(self._by_role),
                "by_model_io": {k: dict(v) for k, v in self._by_model_io.items()},
            }

    # ------------------------------------------------------------------
    # 复制/序列化语义：账本按 run 唯一 ⇒ deepcopy 时共享同一个账本；
    # pickle（若出现）降级为快照复制。避免 threading.Lock 让复制直接失败。
    # ------------------------------------------------------------------
    def __copy__(self) -> TokenLedger:
        return self

    def __deepcopy__(self, memo) -> TokenLedger:  # noqa: ANN001
        return self

    def __getstate__(self) -> Dict[str, Any]:
        return self.snapshot()

    def __setstate__(self, state: Dict[str, Any]) -> None:
        self._lock = threading.Lock()
        self._total = int(state.get("total", 0))
        self._by_model = dict(state.get("by_model", {}))
        self._by_role = dict(state.get("by_role", {}))
        self._by_model_io = {k: dict(v) for k, v in (state.get("by_model_io") or {}).items()}


def ledger_for(run_id: str) -> Optional[TokenLedger]:
    """按 run_id 取账本（未注册/空 id ⇒ None）。"""
    if not run_id:
        return None
    with _REGISTRY_LOCK:
        return _ACTIVE.get(run_id)


def register_ledger(run_id: str, ledger: TokenLedger) -> None:
    """把 run 的账本登记进进程级注册表（run 开始时调用）。"""
    with _REGISTRY_LOCK:
        _ACTIVE[run_id] = ledger


def unregister_ledger(run_id: str) -> None:
    """run 结束时注销（防止注册表随 run 数增长）。"""
    with _REGISTRY_LOCK:
        _ACTIVE.pop(run_id, None)
