"""Per-run runtime primitives（2026-11 架构收敛）。

背景
----
引擎此前把「一次 run 的身份/取消谓词/trace/对账基线」散落在
``DeepResearchGraph`` 的实例属性与 ``run()`` / ``iter_run()`` 的局部变量里，
而节点又把 token 用量**逐节点手写**回 ``state.token_used`` —— 于是漏写一处
（HITL 的 clarify / revise_plan）就会让预算硬闸静默失明。

本包把「一次 run 的运行期上下文」收敛为显式的 :class:`RunContext`，
并用 :func:`with_token_writeback` 把 token 写回从「每个节点自觉」改成
「节点包装器统一保证」——新增节点不可能再漏。

边界（刻意的）
--------------
- ``RunContext`` **不持有** LLM/检索客户端，也不做预算结算；预算的单一事实源
  仍是 ``state.token_used``（由 ``budget.admit_call/settle_call`` 原子维护）。
- 每份 ``RunContext`` 只属于一次 run；多 run 并发（eval 线程池 / 队列 Worker）
  不得共享，调用方负责按 run 新建（见 ``eval/run.py`` 的 per-run graph 修复）。
"""
from __future__ import annotations

from .context import RunContext, new_run_context, with_token_writeback

__all__ = ["RunContext", "new_run_context", "with_token_writeback"]
