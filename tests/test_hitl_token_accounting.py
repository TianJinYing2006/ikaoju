# -*- coding: utf-8 -*-
"""2026-11 回归：HITL 节点必须把 LLM 用量写回 `state.token_used` 的 delta。

背景：``_clarify`` / ``_revise_plan`` 通过 ``router...(state=state)`` 已把用量累加进
``state.token_used``，但节点击返回值漏掉了 ``token_used`` 字段。图通道只合并
显式返回的 delta ⇒ 下一个节点的预算硬闸看到的是**旧值**，HITL 路径的消耗对预算
闸不可见（与「token 进硬闸」的既有契约冲突）。

修复：三个 clarify 分支 + revise_plan 分支均显式写回。
"""
from __future__ import annotations

from research_engine.graph import DeepResearchGraph
from research_engine.state import ResearchState, SubQuestion


def test_clarify_cleared_returns_token_used():
    g = DeepResearchGraph()
    state = ResearchState(topic="主题")

    def _decide(topic, instructions, clarification, state=None):
        state.token_used += 123  # 模拟 clarifier 的 LLM 用量记账
        return {"needed": False, "questions": [], "reason": "clear", "failed": False}

    g.clarifier.decide = _decide
    delta = g._clarify(state)
    assert delta["token_used"] == 123


def test_clarify_questioned_returns_token_used():
    g = DeepResearchGraph()
    state = ResearchState(topic="主题")

    def _decide(topic, instructions, clarification, state=None):
        state.token_used += 55
        return {"needed": True, "questions": ["范围？"], "reason": "", "failed": False}

    g.clarifier.decide = _decide
    delta = g._clarify(state)
    assert delta["token_used"] == 55
    assert delta["clarify_needed"] is True


def test_clarify_failed_returns_token_used():
    g = DeepResearchGraph()
    state = ResearchState(topic="主题")

    def _decide(topic, instructions, clarification, state=None):
        state.token_used += 7
        return {"needed": False, "questions": [], "reason": "llm_error", "failed": True}

    g.clarifier.decide = _decide
    delta = g._clarify(state)
    assert delta["token_used"] == 7
    assert delta["planner_events"][0]["event"] == "clarify_skipped"


def test_revise_plan_returns_token_used():
    g = DeepResearchGraph()
    state = ResearchState(
        topic="主题",
        subquestions=[SubQuestion(id="q1", question="问题一", rationale="r")],
        plan_decision={"action": "revise", "text": "换个角度"},
    )

    def _revise(topic, subs, feedback, state=None):
        state.token_used += 77
        g.planner.last_revise_fallback = False
        return [SubQuestion(id="q1", question="修订后的问题", rationale="r")]

    g.planner.revise_plan = _revise
    g.planner.drain_planner_events = lambda: []
    g.planner.drain_degradations = lambda: []
    delta = g._revise_plan(state)
    assert delta["token_used"] == 77
    assert delta["plan_version"] == state.plan_version + 1
