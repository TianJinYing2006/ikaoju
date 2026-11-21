# -*- coding: utf-8 -*-
"""2026-11：RunContext 与节点 token 写回契约的单测。

锁三件事：
1. RunContext 是一次 run 的显式句柄（run_id/trace/cancel 不再散落）；
2. ``with_token_writeback`` 保证**任何**节点 delta 都带 ``token_used``（新增节点漏写不可能）；
3. 生产图的所有节点都已套上该装饰器（结构性断言，防止有人新增节点忘记）。
"""
from __future__ import annotations

import threading

import pytest

import research_engine.graph as graph_mod
from research_engine.graph import DeepResearchGraph
from research_engine.runtime import RunContext, new_run_context, with_token_writeback
from research_engine.state import ResearchState

NODE_METHODS = [
    "_plan", "_clarify", "_await_clarify", "_await_plan", "_revise_plan",
    "_research", "_critic", "_revise", "_write", "_validate", "_repair", "_render",
]


# ---------------------------------------------------------------- RunContext

def test_new_run_context_generates_run_id():
    ctx = new_run_context()
    assert ctx.run_id.startswith("dr-")
    assert ctx.trace_id is None
    assert ctx.cancelled() is False


def test_new_run_context_keeps_explicit_thread_id():
    ctx = new_run_context("eval-q_001-abc123")
    assert ctx.run_id == "eval-q_001-abc123"


def test_run_context_cancel_predicate():
    flag = threading.Event()
    ctx = RunContext(run_id="r1", cancel=flag.is_set)
    assert ctx.cancelled() is False
    flag.set()
    assert ctx.cancelled() is True


def test_run_context_cancel_predicate_error_propagates():
    """坏谓词不能被静默吞掉（否则取消语义会静默失效）。"""
    def boom() -> bool:
        raise RuntimeError("bad predicate")

    ctx = RunContext(run_id="r1", cancel=boom)
    with pytest.raises(RuntimeError):
        ctx.cancelled()


def test_run_context_as_dict_shape():
    ctx = RunContext(run_id="r1", trace_id="t1")
    payload = ctx.as_dict()
    assert payload["run_id"] == "r1"
    assert payload["trace_id"] == "t1"
    assert isinstance(payload["elapsed_s"], float)


# ---------------------------------------------------------------- 写回装饰器

def test_writeback_injects_token_used():
    class _S:
        token_used = 42

    @with_token_writeback
    def node(self, state):
        return {"status": "ok"}

    delta = node(object(), _S())
    assert delta == {"status": "ok", "token_used": 42}


def test_writeback_preserves_explicit_value():
    class _S:
        token_used = 42

    @with_token_writeback
    def node(self, state):
        return {"token_used": 7}

    assert node(object(), _S())["token_used"] == 7


def test_writeback_marks_wrapper():
    @with_token_writeback
    def node(self, state):
        return {}

    assert getattr(node, "__token_writeback__", False) is True


def test_every_production_node_is_wrapped():
    """结构性守卫：生产图的每个节点都必须带 token 写回（新增节点漏套即红）。"""
    missing = [name for name in NODE_METHODS
               if not getattr(getattr(DeepResearchGraph, name), "__token_writeback__", False)]
    assert missing == [], f"节点未套 with_token_writeback：{missing}"


def test_research_early_return_carries_token_used():
    """_research 的「无可用查询」早退分支历史上漏写 token_used，由包装器兜底。"""
    g = DeepResearchGraph()
    state = ResearchState(topic="主题", frontier=[])
    delta = g._research(state)
    assert "token_used" in delta
    assert delta["token_used"] == state.token_used


# ---------------------------------------------------------------- iter_run 接线

def test_iter_run_passes_run_context_cancel(monkeypatch):
    """iter_run 必须把取消谓词经 RunContext 归一后传给 _iter_steps。"""
    recorded: dict = {}

    def fake_iter_steps(self, stream_input, cfg, should_cancel, token_base, **kw):
        recorded["should_cancel"] = should_cancel
        recorded["token_base"] = token_base
        return iter(())

    monkeypatch.setattr(DeepResearchGraph, "_iter_steps", fake_iter_steps)
    g = DeepResearchGraph.__new__(DeepResearchGraph)
    g.trace_id = None
    g.last_exception = None
    g.last_stop_reason = None

    flag = threading.Event()
    list(g.iter_run("主题", thread_id="t-cancel", should_cancel=flag.is_set))

    assert callable(recorded["should_cancel"])
    assert recorded["should_cancel"]() is False
    flag.set()
    assert recorded["should_cancel"]() is True

    # 不传取消谓词 ⇒ 传 None，而不是恒 False 的包装器（保持取消语义无副作用）
    recorded.clear()
    list(g.iter_run("主题", thread_id="t-no-cancel"))
    assert recorded["should_cancel"] is None


def test_graph_module_uses_run_context_factory(monkeypatch):
    """run()/iter_run() 都必须经 new_run_context 建上下文（单一入口）。"""
    calls: list = []
    real = graph_mod.new_run_context

    def spy(thread_id=None, cancel=None, trace_id=None):
        calls.append((thread_id, cancel is not None))
        return real(thread_id, cancel=cancel, trace_id=trace_id)

    monkeypatch.setattr(graph_mod, "new_run_context", spy)

    def fake_iter_steps(self, *a, **kw):
        return iter(())

    monkeypatch.setattr(DeepResearchGraph, "_iter_steps", fake_iter_steps)
    g = DeepResearchGraph.__new__(DeepResearchGraph)
    g.trace_id = None
    g.last_exception = None
    g.last_stop_reason = None
    list(g.iter_run("主题", thread_id="t-spy"))
    assert calls == [("t-spy", False)]
