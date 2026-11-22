# -*- coding: utf-8 -*-
"""2026-11：运行档位（RuntimeProfile）显式注入。

验收：
1. `create_graph(profile=...)` 带能力标记并保存档位；
2. `run()/iter_run()` 在 invoke/stream 期间让档位生效（节点读到的就是显式传入的那份），
   结束后恢复原 contextvar —— 不把档位泄漏给同线程的下一次 run；
3. worker 默认 factory 支持 `profile=` 透传（能力探测路径）。
"""
from __future__ import annotations

from types import SimpleNamespace

from research_engine.graph import DeepResearchGraph, create_graph
from research_engine.runtime_profile import current_profile


class _FakeInvokeGraph:
    def __init__(self):
        self.seen_profile = None

    def invoke(self, initial, cfg):
        self.seen_profile = current_profile()
        return {"topic": initial.topic}


class _FakeStreamGraph:
    def __init__(self):
        self.seen_profile = None

    def stream(self, stream_input, cfg, stream_mode="values"):
        self.seen_profile = current_profile()
        yield {"topic": "t"}


def test_create_graph_accepts_profile():
    profile = SimpleNamespace(name="quick")
    g = create_graph(profile=profile)
    assert getattr(create_graph, "accepts_profile", False) is True
    assert g._profile is profile


def test_run_applies_profile_and_restores_contextvar():
    profile = SimpleNamespace(name="quick")
    g = DeepResearchGraph(profile=profile)
    fake = _FakeInvokeGraph()
    g.graph = fake
    out = g.run("主题")
    assert fake.seen_profile is profile, "run() invoke 期间必须使用显式传入的档位"
    assert out.topic == "主题"
    assert current_profile() is None, "run 结束后必须恢复原 contextvar（不泄漏给下一次 run）"


def test_iter_run_applies_profile_and_restores_contextvar():
    profile = SimpleNamespace(name="deep")
    g = DeepResearchGraph(profile=profile)
    fake = _FakeStreamGraph()
    g.graph = fake
    steps = list(g.iter_run("主题", thread_id="profile-iter"))
    assert fake.seen_profile is profile
    assert steps[-1].terminal is True
    assert current_profile() is None


def test_worker_factory_forwards_profile(monkeypatch):
    import web.backend.worker as worker_module

    captured = {}

    def fake_create_graph(checkpointer=None, rag_scope=None, profile=None):
        captured.update(checkpointer=checkpointer, rag_scope=rag_scope, profile=profile)
        return object()

    monkeypatch.setattr(worker_module, "create_graph", fake_create_graph)
    factory = worker_module.make_graph_factory()
    assert getattr(factory, "accepts_profile", False) is True
    profile = SimpleNamespace(name="standard")
    factory(profile=profile)
    assert captured["profile"] is profile

def test_legacy_set_profile_is_not_reset_when_not_injected():
    """2026-11 PR 审阅修复：未显式注入档位时不得覆盖调用方 set_profile 的旧约定。"""
    from research_engine.runtime_profile import current_profile, set_profile

    profile = SimpleNamespace(
        name="legacy-quick", max_total_hops=20, max_subquestions=4,
        token_budget=100000, max_replan=1, per_subq_hop_cap=5)
    g = DeepResearchGraph()  # 不传 profile ⇒ _UNSET
    fake = _FakeInvokeGraph()
    g.graph = fake
    set_profile(profile)
    try:
        g.run("主题")
        assert fake.seen_profile is profile, "旧 set_profile 约定必须继续生效"
        assert current_profile() is profile
    finally:
        set_profile(None)