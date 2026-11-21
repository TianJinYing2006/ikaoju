"""2026-11：run 级 token 账本（替代引擎对类级全局计数器的运行期依赖）。

验收：
1. TokenLedger 累加正确、并发安全、快照不可变副本；
2. LLMClient 记账按 ``state.run_id`` 找到本 run 的账本——两个 run 互不污染；
3. RunContext 自带独立账本实例；注册表可注销且不串 id。
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

from research_engine.llm.client import LLMClient
from research_engine.runtime import RunContext, TokenLedger
from research_engine.runtime.ledger import (
    ledger_for,
    register_ledger,
    unregister_ledger,
)
from research_engine.state import ResearchState


def _resp(total=100, inp=80, out=20):
    return SimpleNamespace(usage=SimpleNamespace(
        total_tokens=total, prompt_tokens=inp, completion_tokens=out), id="resp-1")


def test_ledger_accumulates_and_snapshots():
    ledger = TokenLedger()
    ledger.add(model="qwen-plus", role="critic", input_tokens=80, output_tokens=20, total_tokens=100)
    ledger.add(model="qwen-plus", role="smart", input_tokens=30, output_tokens=10, total_tokens=40)
    snap = ledger.snapshot()
    assert snap["total"] == 140
    assert snap["by_model"] == {"qwen-plus": 140}
    assert snap["by_role"] == {"critic": 100, "smart": 40}
    assert snap["by_model_io"] == {"qwen-plus": {"input": 110, "output": 30}}
    ledger.add(model="qwen-plus", role="critic", total_tokens=1)
    assert snap["total"] == 140, "快照必须是副本"


def test_ledger_is_thread_safe():
    ledger = TokenLedger()

    def worker():
        for _ in range(1000):
            ledger.add(model="m", role="r", total_tokens=1)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert ledger.total == 4000


def test_run_context_has_own_ledger():
    a = RunContext(run_id="a")
    b = RunContext(run_id="b")
    a.ledger.add(model="m", role="r", total_tokens=5)
    assert a.ledger.total == 5
    assert b.ledger.total == 0


def test_registry_register_lookup_unregister():
    ledger = TokenLedger()
    register_ledger("run-1", ledger)
    try:
        assert ledger_for("run-1") is ledger
        assert ledger_for("missing") is None
        assert ledger_for("") is None
    finally:
        unregister_ledger("run-1")
    assert ledger_for("run-1") is None


def test_llm_client_records_into_run_ledger():
    ledger = TokenLedger()
    register_ledger("run-ledger", ledger)
    try:
        state = ResearchState(topic="t", run_id="run-ledger")
        client = LLMClient(model="qwen-plus", role="critic")
        client._accumulate_usage(_resp(), state)
        snap = ledger.snapshot()
        assert snap["total"] == 100
        assert snap["by_role"] == {"critic": 100}
    finally:
        unregister_ledger("run-ledger")


def test_ledgers_are_isolated_between_runs():
    s1 = ResearchState(topic="t1", run_id="run-a")
    s2 = ResearchState(topic="t2", run_id="run-b")
    l1, l2 = TokenLedger(), TokenLedger()
    register_ledger("run-a", l1)
    register_ledger("run-b", l2)
    try:
        client = LLMClient(model="qwen-plus", role="critic")
        client._accumulate_usage(_resp(total=100, inp=80, out=20), s1)
        client._accumulate_usage(_resp(total=7, inp=5, out=2), s2)
        assert l1.total == 100
        assert l2.total == 7
    finally:
        unregister_ledger("run-a")
        unregister_ledger("run-b")


def test_llm_client_without_registered_ledger_does_not_crash():
    """未注册账本（评测裁判 / 旧调用 / 恢复后的新进程）时照旧记账，不得抛错。"""
    state = ResearchState(topic="t", run_id="")
    client = LLMClient(model="qwen-plus", role="judge")
    client._accumulate_usage(_resp(), state)
    assert state.token_used > 0
