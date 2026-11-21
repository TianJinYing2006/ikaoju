# -*- coding: utf-8 -*-
"""2026-11：评测确定性 —— 网络搜索 replay provider（录制/回放）与 eval CLI 接线。

验收点：
1. 同一 fixture 下检索结果确定、可复算（零网络）；
2. 未录制 query / 录制失败 均**可归因**（empty_result / 原 failure_reason），不返回假数据；
3. fixture 损坏 fail-fast（构造即 ValueError）；
4. factory 只在显式 replay 时启用，且不污染用户可选 Provider 清单（KNOWN_PROVIDERS）；
5. eval CLI 的 ``--replay-dir`` 同时关闭 arXiv（否则确定性不完整）。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from config import config
from research_engine.eval.run import _apply_replay_overrides
from research_engine.search.base import KNOWN_PROVIDERS, create_search_provider
from research_engine.search.replay import FIXTURE_NAME, ReplaySearchProvider


def _write_fixture(tmp_path: Path, payload: dict) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / FIXTURE_NAME).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def _fixture(**overrides) -> dict:
    payload = {
        "version": 1,
        "queries": {
            "RAG 进展": {
                "results": [
                    {"title": "A", "url": "https://a.example/1", "snippet": "s1",
                     "source": "web", "metadata": {"k": "v"}},
                    {"title": "B", "url": "https://b.example/2", "snippet": "s2"},
                    {"title": "C", "url": "https://c.example/3", "snippet": "s3"},
                ],
            },
            "失败查询": {"results": [], "failure_reason": "timeout",
                         "failure_detail": "recorded timeout"},
            "空查询": {"results": []},
        },
    }
    payload["queries"].update(overrides.get("queries", {}))
    return payload


def test_replay_returns_recorded_results(tmp_path):
    provider = ReplaySearchProvider(_write_fixture(tmp_path, _fixture()))
    resp = provider.search("RAG 进展", max_results=8)
    assert resp.ok is True
    assert [r.title for r in resp.results] == ["A", "B", "C"]
    assert resp.results[0].metadata == {"k": "v"}
    assert resp.results[1].source == "web"


def test_replay_is_deterministic(tmp_path):
    provider = ReplaySearchProvider(_write_fixture(tmp_path, _fixture()))
    first = [(r.title, r.url, r.snippet) for r in provider.search("RAG 进展").results]
    second = [(r.title, r.url, r.snippet) for r in provider.search("RAG 进展").results]
    assert first == second


def test_replay_truncates_to_max_results(tmp_path):
    provider = ReplaySearchProvider(_write_fixture(tmp_path, _fixture()))
    assert len(provider.search("RAG 进展", max_results=2).results) == 2
    assert provider.search("RAG 进展", max_results=0).results == []


def test_replay_unrecorded_query_is_attributable(tmp_path):
    provider = ReplaySearchProvider(_write_fixture(tmp_path, _fixture()))
    resp = provider.search("没有录过的问题")
    assert resp.ok is False
    assert resp.failure_reason == "empty_result"
    assert "没有录过的问题" in resp.failure_detail


def test_replay_passes_through_recorded_failure(tmp_path):
    provider = ReplaySearchProvider(_write_fixture(tmp_path, _fixture()))
    resp = provider.search("失败查询")
    assert resp.ok is False
    assert resp.failure_reason == "timeout"
    assert resp.failure_detail == "recorded timeout"


def test_replay_missing_fixture_fails_fast(tmp_path):
    with pytest.raises(ValueError, match="fixture 不存在"):
        ReplaySearchProvider(tmp_path / "missing")


def test_replay_invalid_failure_reason_fails_fast(tmp_path):
    payload = _fixture(queries={"坏查询": {"results": [], "failure_reason": "not_a_reason"}})
    with pytest.raises(ValueError, match="failure_reason 非法"):
        ReplaySearchProvider(_write_fixture(tmp_path, payload))


def test_replay_invalid_result_item_fails_fast(tmp_path):
    payload = _fixture(queries={"坏条目": {"results": [{"title": "x"}]}})
    with pytest.raises(ValueError, match="缺少 title/url/snippet"):
        ReplaySearchProvider(_write_fixture(tmp_path, payload))


def test_factory_builds_replay_only_with_dir(tmp_path, monkeypatch):
    fixture_dir = _write_fixture(tmp_path, _fixture())
    monkeypatch.setattr(config.search, "replay_dir", str(fixture_dir))
    provider = create_search_provider("replay")
    assert isinstance(provider, ReplaySearchProvider)
    assert provider.search("失败查询").failure_reason == "timeout"

    monkeypatch.setattr(config.search, "replay_dir", "")
    with pytest.raises(ValueError, match="DR_REPLAY_DIR"):
        create_search_provider("replay")


def test_replay_is_not_user_selectable_provider():
    """replay 是评测能力，不能出现在 /api/options 的用户可选清单里。"""
    assert "replay" not in KNOWN_PROVIDERS


def test_eval_replay_override_disables_arxiv(tmp_path, monkeypatch):
    fixture_dir = _write_fixture(tmp_path, _fixture())
    monkeypatch.setattr(config.search, "provider", "bocha")
    monkeypatch.setattr(config.search, "replay_dir", "")
    monkeypatch.setattr(config.search, "enable_arxiv", True)

    _apply_replay_overrides(argparse.Namespace(replay_dir=fixture_dir))
    assert config.search.provider == "replay"
    assert config.search.replay_dir == str(fixture_dir)
    assert config.search.enable_arxiv is False


def test_eval_replay_override_noop_without_arg(monkeypatch):
    monkeypatch.setattr(config.search, "provider", "bocha")
    monkeypatch.setattr(config.search, "replay_dir", "")
    _apply_replay_overrides(argparse.Namespace(replay_dir=None))
    assert config.search.provider == "bocha"
    assert config.search.replay_dir == ""


def test_eval_replay_override_rejects_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config.search, "provider", "bocha")
    monkeypatch.setattr(config.search, "replay_dir", "")
    monkeypatch.setattr(config.search, "enable_arxiv", True)
    with pytest.raises(SystemExit, match="不存在"):
        _apply_replay_overrides(argparse.Namespace(replay_dir=tmp_path / "nope"))
    # fail-fast 不得留下「部分覆盖」，否则同进程后续 run 会静默变成 replay。
    assert config.search.provider == "bocha"
    assert config.search.replay_dir == ""
    assert config.search.enable_arxiv is True
