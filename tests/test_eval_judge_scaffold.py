# -*- coding: utf-8 -*-
"""2026-11：eval 独立裁判脚手架（配置 + provenance + CLI 覆盖）。

不调用任何模型：只验证「谁裁的 / 是否可证明独立」的配置与 provenance 链路。
独立性判据是**声明式**的（显式 judge_model + 显式 judge_family 且与主链路模型族不同），
未配置时一律 False —— 不允许从模型名推断「独立」。
"""
from __future__ import annotations

import argparse

from config import config
from research_engine.eval.metrics import judge_model_name
from research_engine.eval.provenance import citation_judge_is_independent, judge_provenance
from research_engine.eval.run import _apply_judge_overrides


def _reset_judge_config(monkeypatch) -> None:
    monkeypatch.setattr(config.eval, "judge_model", "")
    monkeypatch.setattr(config.eval, "judge_family", "")


def test_judge_model_defaults_to_smart_model(monkeypatch):
    _reset_judge_config(monkeypatch)
    assert judge_model_name() == config.llm.smart_model
    assert judge_provenance()["coverage_judge_model"] == config.llm.smart_model
    assert citation_judge_is_independent() is False


def test_judge_model_follows_explicit_config(monkeypatch):
    _reset_judge_config(monkeypatch)
    monkeypatch.setattr(config.eval, "judge_model", "deepseek-chat")
    assert judge_model_name() == "deepseek-chat"
    # 只配模型、未声明模型族 ⇒ 不宣称独立
    assert citation_judge_is_independent() is False


def test_configured_judge_does_not_claim_independence(monkeypatch):
    """2026-11 PR 审阅修复：配置不同模型族**不能**把独立性置 true。

    事实是 headline citation_accuracy 仍直读主链路 validator；配置只影响另起的
    裁判实例，不构成独立复判。该字段必须恒 False，直到实现真正的独立复判。
    """
    _reset_judge_config(monkeypatch)
    monkeypatch.setattr(config.eval, "judge_model", "qwen-max")
    monkeypatch.setattr(config.eval, "judge_family", "qwen")  # 与主链路同族
    assert citation_judge_is_independent() is False

    monkeypatch.setattr(config.eval, "judge_family", "deepseek")  # 声明不同族也不行
    assert citation_judge_is_independent() is False
    assert judge_provenance()["citation_judge_independent"] is False


def test_cli_overrides_are_applied(monkeypatch):
    _reset_judge_config(monkeypatch)
    args = argparse.Namespace(judge_model="deepseek-chat", judge_family="deepseek")
    _apply_judge_overrides(args)
    assert config.eval.judge_model == "deepseek-chat"
    assert config.eval.judge_family == "deepseek"
    assert judge_model_name() == "deepseek-chat"
    # 配置被记录，但不改写「裁判是否独立」的事实
    assert citation_judge_is_independent() is False


def test_cli_empty_values_do_not_override(monkeypatch):
    _reset_judge_config(monkeypatch)
    monkeypatch.setattr(config.eval, "judge_model", "kept-model")
    args = argparse.Namespace(judge_model=None, judge_family="")
    _apply_judge_overrides(args)
    assert config.eval.judge_model == "kept-model"
    assert config.eval.judge_family == ""


# ---------------------------------------------------------------- 2026-11 裁判账本

def test_judge_with_ledger_injects_state_and_registers():
    from research_engine.eval.metrics import JudgeWithLedger
    from research_engine.runtime import TokenLedger
    from research_engine.runtime.ledger import ledger_for

    class _FakeClient:
        def __init__(self):
            self.seen_state = None
            self.seen_messages = None

        def chat_json(self, messages, state=None, **kwargs):
            self.seen_state = state
            self.seen_messages = messages
            return {"ok": True}

    fake = _FakeClient()
    ledger = TokenLedger()
    judge = JudgeWithLedger(fake, ledger, "eval-judge-test")
    assert ledger_for("eval-judge-test") is ledger
    assert judge.chat_json([{"role": "user"}]) == {"ok": True}
    assert fake.seen_state is not None
    assert fake.seen_state.run_id == "eval-judge-test"
    judge.close()
    assert ledger_for("eval-judge-test") is None


def test_judge_with_ledger_keeps_explicit_state():
    from research_engine.eval.metrics import JudgeWithLedger
    from research_engine.runtime import TokenLedger

    class _FakeClient:
        def chat(self, messages, state=None, **kwargs):
            return state

    judge = JudgeWithLedger(_FakeClient(), TokenLedger(), "eval-judge-x")
    explicit = object()
    assert judge.chat([], state=explicit) is explicit
    judge.close()


def test_make_judge_returns_ledgered_wrapper():
    from research_engine.eval.metrics import JudgeWithLedger, _make_judge

    judge = _make_judge()
    try:
        assert isinstance(judge, JudgeWithLedger)
        assert judge.ledger.total == 0
    finally:
        judge.close()
