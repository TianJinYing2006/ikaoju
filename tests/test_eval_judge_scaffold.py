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


def test_independence_requires_different_declared_family(monkeypatch):
    _reset_judge_config(monkeypatch)
    monkeypatch.setattr(config.eval, "judge_model", "qwen-max")
    monkeypatch.setattr(config.eval, "judge_family", "qwen")  # 与主链路同族
    assert citation_judge_is_independent() is False

    monkeypatch.setattr(config.eval, "judge_family", "deepseek")  # 声明不同族
    assert citation_judge_is_independent() is True
    assert judge_provenance()["citation_judge_independent"] is True


def test_cli_overrides_are_applied(monkeypatch):
    _reset_judge_config(monkeypatch)
    args = argparse.Namespace(judge_model="deepseek-chat", judge_family="deepseek")
    _apply_judge_overrides(args)
    assert config.eval.judge_model == "deepseek-chat"
    assert config.eval.judge_family == "deepseek"
    assert judge_model_name() == "deepseek-chat"
    assert citation_judge_is_independent() is True


def test_cli_empty_values_do_not_override(monkeypatch):
    _reset_judge_config(monkeypatch)
    monkeypatch.setattr(config.eval, "judge_model", "kept-model")
    args = argparse.Namespace(judge_model=None, judge_family="")
    _apply_judge_overrides(args)
    assert config.eval.judge_model == "kept-model"
    assert config.eval.judge_family == ""
