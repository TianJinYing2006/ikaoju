# -*- coding: utf-8 -*-
"""2026-11 PR 审阅修复：Phase1 成本不完整时必须显式 cost_degraded（不静默低估）。"""
from __future__ import annotations

import json


def test_cost_block_propagates_phase1_degraded(tmp_path):
    from research_engine.eval.run import _build_cost_block

    run_dir = tmp_path / "run_x"
    run_dir.mkdir()
    (run_dir / "phase1_global_stats.json").write_text(json.dumps({
        "tokens_total": 123,
        "model_stats": {"qwen-plus": 123},
        "model_io_stats": {"qwen-plus": {"input": 100, "output": 23}},
        "role_stats": {"critic": 123},
        "cost_degraded": True,
        "cost_degraded_reason": "timeout_items=1: ['q_001']",
    }), encoding="utf-8")

    block = _build_cost_block(run_dir)
    assert block["cost_degraded"] is True
    assert "timeout_items" in block["cost_degraded_reason"]
    assert block["total_tokens"] > 0
