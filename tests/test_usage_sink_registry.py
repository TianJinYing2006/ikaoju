# -*- coding: utf-8 -*-
"""2026-11：usage sink 的显式注册表（主链路去 contextvar 的第一步）。

验收：
1. 按 run_id 注册/注销；emit 带 run_id 时优先走注册表；
2. 注册表 sink 与 contextvar sink 同时存在时只投递一次（显式优先）；
3. 未注册 run_id 自动回退 contextvar（兼容旧调用/单测）；
4. sink 失败计数 + strict 上抛语义对注册表 sink 同样生效。
"""
from __future__ import annotations

import pytest

from research_engine.usage import (
    UsageRecord,
    UsageSinkError,
    emit_usage,
    register_usage_sink,
    sink_failure_count,
    unregister_usage_sink,
    usage_sink_for,
    use_usage_sink,
)


def _record() -> UsageRecord:
    return UsageRecord(kind="llm", provider="dashscope", model="qwen-plus",
                       role="critic", total_tokens=10)


def test_register_lookup_unregister():
    sink = lambda _r: None  # noqa: E731
    register_usage_sink("run-1", sink)
    try:
        assert usage_sink_for("run-1") is sink
        assert usage_sink_for("missing") is None
        assert usage_sink_for("") is None
    finally:
        unregister_usage_sink("run-1")
    assert usage_sink_for("run-1") is None


def test_register_requires_run_id():
    with pytest.raises(ValueError):
        register_usage_sink("", lambda _r: None)


def test_emit_prefers_registry_and_delivers_once():
    explicit: list[UsageRecord] = []
    legacy: list[UsageRecord] = []
    register_usage_sink("run-2", explicit.append)
    try:
        with use_usage_sink(legacy.append):
            emit_usage(_record(), run_id="run-2")
        assert len(explicit) == 1
        assert legacy == [], "显式 run_id 命中注册表时不得再投 contextvar（避免双记）"
    finally:
        unregister_usage_sink("run-2")


def test_emit_falls_back_to_contextvar_without_run_id():
    legacy: list[UsageRecord] = []
    with use_usage_sink(legacy.append):
        emit_usage(_record(), run_id="unregistered")
    assert len(legacy) == 1


def test_emit_noop_without_any_sink():
    emit_usage(_record())
    emit_usage(_record(), run_id="nobody")


def test_registry_sink_failure_counts_and_strict_raises(monkeypatch):
    def boom(_record):
        raise RuntimeError("ledger down")

    before = sink_failure_count()
    register_usage_sink("run-boom", boom)
    try:
        emit_usage(_record(), run_id="run-boom")  # 默认不抛
        assert sink_failure_count() >= before + 1
        monkeypatch.setenv("DR_USAGE_STRICT", "true")
        with pytest.raises(UsageSinkError):
            emit_usage(_record(), run_id="run-boom")
    finally:
        unregister_usage_sink("run-boom")
