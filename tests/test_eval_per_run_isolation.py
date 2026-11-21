# -*- coding: utf-8 -*-
"""2026-11 回归：eval Phase 1 必须 **per-run** 建图，禁止线程池共享同一 graph 实例。

背景：``DeepResearchGraph`` 的 Agent 持有实例可变状态（Validator 的
``last_validation_stats``、各 Agent 的 degradation 缓冲）。旧实现
``graph = create_graph()`` 后把同一实例交给 3 线程共享 ⇒ 并发题目的验证统计 /
降级记录互相串台，直接污染评测证据。修复后 ``_run_one(graph=None)`` 在每个
运行线程内新建图实例。

本测试锁两件事：
1. 缺省 ``graph=None`` 时会调用 ``create_graph()``（且只用一份）；
2. 并发跑多条时，**每条 run 用自己线程内创建的那份实例**（实例与线程一一对应）。
"""
from __future__ import annotations

import itertools
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from research_engine.eval import run as run_mod


class _FakeState:
    token_used = 0
    status = "done"
    error = None

    def model_dump(self):
        return {"status": self.status, "report": "ok"}


class _FakeGraph:
    _seq = itertools.count()

    def __init__(self, owner_thread: str):
        self.instance_id = next(_FakeGraph._seq)
        self.owner_thread = owner_thread
        self.run_threads: list[str] = []

    def run(self, topic, thread_id=None):  # noqa: ANN001 - 与真实签名兼容即可
        self.run_threads.append(threading.current_thread().name)
        return _FakeState()


@pytest.fixture()
def _fake_factory(monkeypatch):
    """把 run 模块的 create_graph 换成记录实例的工厂。"""
    instances: list[_FakeGraph] = []
    lock = threading.Lock()

    def _factory():
        inst = _FakeGraph(threading.current_thread().name)
        with lock:
            instances.append(inst)
        return inst

    monkeypatch.setattr(run_mod, "create_graph", _factory)
    return instances


def _call(tmp_path: Path, graph=None):
    return run_mod._run_one(
        {"id": "q_iso", "query": "topic"},
        {"version": "1.1"},
        tmp_path,
        graph,
        {},
    )


def test_run_one_creates_graph_when_not_provided(tmp_path, _fake_factory):
    """缺省 graph=None ⇒ 在当前线程新建一份图（不再依赖调用方传入共享实例）。"""
    res = _call(tmp_path)
    assert res["invoke_status"] == "ok"
    assert len(_fake_factory) == 1
    assert _fake_factory[0].run_threads == [threading.current_thread().name]


def test_run_one_uses_explicit_graph_when_provided(tmp_path, _fake_factory):
    """显式传图（单测/外部调用）时不得再新建，保持向后兼容。"""
    explicit = _FakeGraph(threading.current_thread().name)
    res = _call(tmp_path, explicit)
    assert res["invoke_status"] == "ok"
    assert _fake_factory == []
    assert explicit.run_threads


def test_concurrent_runs_each_use_own_thread_local_graph(tmp_path, _fake_factory):
    """并发 3 条：3 份实例、每份只被创建它的那个线程使用。"""
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _i: _call(tmp_path), range(3)))

    assert [r["invoke_status"] for r in results] == ["ok", "ok", "ok"]
    assert len(_fake_factory) == 3, "必须每 run 一份图实例，不能复用共享实例"
    assert len({inst.instance_id for inst in _fake_factory}) == 3
    for inst in _fake_factory:
        # 实例由哪个线程创建，就必须只被哪个线程使用（线程内建图）
        assert set(inst.run_threads) == {inst.owner_thread}, (
            f"graph {inst.instance_id} 被跨线程使用：{inst.run_threads}"
        )
