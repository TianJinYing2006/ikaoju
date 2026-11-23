"""LLM 客户端封装。

基于 OpenAI 兼容接口调用阿里云百炼 Qwen 系列模型。
所有 Agent 通过本模块访问 LLM，统一处理结构化输出与错误降级。
客户端懒加载，图构建时不强制需要 API key。
"""
from __future__ import annotations

import json
import os
import random
import time
from typing import Any, Callable, Dict, List, Optional

from openai import OpenAI
from pydantic import BaseModel, ValidationError

from config import config
from research_engine.budget import (
    DeadlineExceeded,
    add_charged_usage,
    estimate_messages_tokens,
    reserve_call,
    settle_call,
)
from research_engine.failure_reasons import is_provider_exception
from research_engine.observability import llm_call_span, record_llm_result
from research_engine.runtime_profile import task_deadline_remaining
from research_engine.usage import UsageRecord, emit_usage

#: #310：有任务时限时 SDK 自带重试被关闭（不得越过整场 deadline），由下面的有界重试补偿。
#: 只重试 provider 侧瞬时错误（429/5xx/网络），且剩余时限必须大于「退避 + 最小调用余量」。
_RETRY_MIN_REMAINING_SECONDS = 5.0


def _llm_retry_max() -> int:
    """有界重试次数（默认 2，clamp 0..5）。"""
    try:
        value = int(os.getenv("DR_LLM_RETRY_MAX", "2"))
    except ValueError:
        value = 2
    return max(0, min(5, value))


def _retry_delay_seconds(attempt: int) -> float:
    """指数退避 + 抖动（base 默认 2s，单次上限 60s）。"""
    try:
        base = float(os.getenv("DR_LLM_RETRY_BASE_SECONDS", "2"))
    except ValueError:
        base = 2.0
    base = max(0.5, min(30.0, base))
    return min(base * (2 ** attempt), 60.0) + random.uniform(0.0, 0.5)


class LLMClient:
    """OpenAI 兼容的 LLM 客户端，封装百炼 Qwen 调用。

    2026-11：不再维护类级全局计数器。token 统计走 run 级 ``TokenLedger``
    （``state.run_id`` → ``runtime/ledger`` 注册表；judge 用独立账本），
    ``state.token_used`` 仍是预算口径。这样并发 run / 评测 Phase 1/2 互不污染。
    """

    def __init__(self, model: Optional[str] = None, role: Optional[str] = None):
        self.model = model or config.llm.smart_model
        self.role = role or "unknown"
        self._client: Optional[OpenAI] = None

    def _get_client(self) -> OpenAI:
        """懒加载客户端。"""
        if self._client is None:
            if not config.llm.api_key:
                raise RuntimeError("未配置 DASHSCOPE_API_KEY，无法调用 LLM")
            self._client = OpenAI(
                base_url=config.llm.base_url,
                api_key=config.llm.api_key,
            )
        return self._client

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[Dict[str, str]] = None,
        state: Optional[Any] = None,
        timeout: Optional[float] = None,
    ) -> str:
        """普通对话，返回文本内容。state 传入时把 token 用量累加进 state.token_used（Q6-B）。

        W3（Q3=D' 历史口径，2026-11 已废止）：曾用类级计数器对账漏传 state 的调用；
        现在记账只走 ``state.token_used`` + run 级账本（``state.run_id``）。
        W5（Q4）：timeout 透传给 openai SDK（judge 调用设 60s 治"LLM 层无超时"僵尸源）。
        F13（审计）：调用前预算准入（`admit_call`）；未显式传 timeout 时使用默认值并按
        任务剩余时限收窄（时限已过抛 `DeadlineExceeded`，不发起调用）。
        R05（审计）：state 非空时按「输入估计 + 输出上限」**原子预占**额度并把输出上限
        写进 `max_tokens`；调用结束按真实用量结算。缺 usage 时按预占保守计入，
        并发 / 重试不再能一起越过预算线。任务时限存在时禁用 SDK 内部重试
        （`max_retries=0`），保证单次调用（含重试）不越过整场 deadline。
        #310（2026-10-31）：禁用 SDK 重试后，由本方法自带**有界重试**补偿 provider 瞬时
        429/5xx/网络错误（默认 2 次、指数退避 + 抖动），且剩余时限不足时不再重试；
        非 provider 错误（400 等）不重试。流式路径不重试（避免半截输出重复）。
        """
        # 先解析时限（已过期直接拒绝，不预占、不调用），再预占预算
        has_deadline = task_deadline_remaining() is not None
        base_kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else config.llm.temperature,
        }
        if response_format:
            base_kwargs["response_format"] = response_format

        max_retries = _llm_retry_max() if has_deadline else 0
        attempt = 0
        # 需求 33（TBD-2）：generation 单点 span —— 唯一 LLM 出口在此，未启用时 no-op
        with llm_call_span(self.model, role=self.role, messages=messages) as gen_span:
            while True:
                resolved_timeout = self._resolve_timeout(timeout)  # 每次尝试重算（deadline 会缩短）
                reservation = None
                if state is not None:
                    reservation = reserve_call(
                        state,
                        input_tokens=estimate_messages_tokens(messages),
                        desired_output=max_tokens,
                    )
                kwargs = dict(base_kwargs)
                if reservation is not None:
                    kwargs["max_tokens"] = reservation.output_tokens
                elif max_tokens:
                    kwargs["max_tokens"] = max_tokens
                if resolved_timeout is not None:
                    kwargs["timeout"] = resolved_timeout
                # ⚠️ 不变量：预算准入 / 时限检查**先于** SDK 客户端获取——
                # 预算耗尽或 deadline 过期时不得初始化 SDK（既有单测锁死）。
                # 2026-11 审计 #340a（D-09）：预占已在上方发生；`_get_client` 失败
                # （如未配置 API key）时若直接冒泡，reserved_tokens 会永久留在
                # state.token_used（多计）。这里失败即按预占口径结算后再抛。
                try:
                    client = self._get_client()
                except BaseException:
                    # 客户端初始化失败发生在任何网络调用之前 ⇒ 真实用量 0，全额释放预占
                    # （与「调用中途失败按预占保守计入」不同；否则每次配置缺失都会虚增预算）。
                    settle_call(reservation, 0)
                    raise
                if has_deadline:
                    client = client.with_options(max_retries=0)
                try:
                    resp = client.chat.completions.create(**kwargs)
                    break
                except BaseException as exc:
                    # 调用失败（超时/限流/网络）：预占按保守口径计入（可能已消耗输入 token）。
                    settle_call(reservation)
                    if attempt >= max_retries or not is_provider_exception(exc):
                        raise
                    delay = _retry_delay_seconds(attempt)
                    remaining = task_deadline_remaining()
                    if remaining is not None and remaining <= delay + _RETRY_MIN_REMAINING_SECONDS:
                        raise  # 剩余时限不足：不把调用冒险推到 deadline 之后
                    time.sleep(delay)
                    attempt += 1
            content = resp.choices[0].message.content or ""
            record_llm_result(gen_span, resp, content)  # fail-silent：观测旁路
        self._accumulate_usage(resp, state, reservation)  # D'：无条件调用（内部处理 state=None）
        return content

    def chat_stream(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        state: Optional[Any] = None,
        timeout: Optional[float] = None,
        on_delta: Optional[Callable[[str], None]] = None,
    ) -> str:
        """流式对话（DashScope OpenAI 兼容），返回完整文本。

        需求 35 R4 / TBD-6：与 :meth:`chat` **同口径**的准入 / 记账 / 时限，仅传输层换流式：

        - 预算预占 / 结算、``state.token_used``、usage 账本与 chat 共用 ``_accumulate_usage``；
        - 每收到一段增量调用 ``on_delta(text)``（sink 侧 fail-silent，不影响本调用）；
        - 调用失败（含**中途断流**）：与 chat 相同——预占按保守口径计入后原样抛出，
          半截文本不返回（调用方决定降级；预览由 ``report_stream`` 的 reset 协议收口）。
        """
        has_deadline = task_deadline_remaining() is not None
        resolved_timeout = self._resolve_timeout(timeout)
        reservation = None
        if state is not None:
            reservation = reserve_call(
                state,
                input_tokens=estimate_messages_tokens(messages),
                desired_output=max_tokens,
            )
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else config.llm.temperature,
            "stream": True,
            # OpenAI 兼容语义：末帧携带 usage；缺 usage 时按预占保守结算（R05 口径）
            "stream_options": {"include_usage": True},
        }
        if reservation is not None:
            kwargs["max_tokens"] = reservation.output_tokens
        elif max_tokens:
            kwargs["max_tokens"] = max_tokens
        if resolved_timeout is not None:
            kwargs["timeout"] = resolved_timeout

        client = self._get_client()
        with llm_call_span(self.model, role=self.role, messages=messages) as gen_span:
            usage_chunk = None
            parts: List[str] = []
            try:
                if has_deadline:
                    client = client.with_options(max_retries=0)
                stream = client.chat.completions.create(**kwargs)
                for chunk in stream:
                    if getattr(chunk, "usage", None) is not None:
                        usage_chunk = chunk
                    choices = getattr(chunk, "choices", None) or []
                    if not choices:
                        continue
                    delta = getattr(choices[0], "delta", None)
                    text = (getattr(delta, "content", None) or "") if delta is not None else ""
                    if text:
                        parts.append(text)
                        if on_delta is not None:
                            on_delta(text)
            except BaseException:
                # 中途失败：预占按保守口径计入（可能已消耗输入/部分输出），原样抛出
                settle_call(reservation)
                raise
            content = "".join(parts)
            record_llm_result(gen_span, usage_chunk, content)  # fail-silent：观测旁路
        self._accumulate_usage(usage_chunk, state, reservation)
        return content

    @staticmethod
    def _resolve_timeout(explicit: Optional[float]) -> Optional[float]:
        """F13/R05：显式 timeout 与默认值都必须服从任务剩余时限。

        - 任务时限已过 ⇒ 抛 :class:`DeadlineExceeded`（不发起调用）；
        - 有剩余时限 ⇒ 返回 ``min(显式或默认, 剩余秒数)`` —— **显式 timeout 也不能越界**；
        - 无时限 ⇒ 显式 timeout 优先，否则用默认值（0 = 不设）。
        """
        base = float(getattr(config.llm, "request_timeout_seconds", 0.0) or 0.0)
        remaining = task_deadline_remaining()
        if remaining is None:
            if explicit is not None:
                return explicit
            return base if base > 0 else None
        if remaining <= 0:
            raise DeadlineExceeded("task deadline reached before LLM call")
        if explicit is not None:
            return min(float(explicit), remaining)
        return min(base, remaining) if base > 0 else remaining

    def _accumulate_usage(self, resp, state, reservation=None) -> None:
        """把响应里的 token 用量累加进 state.token_used（Q6-B 可观测+控闸）。

        2026-11：记账只走两条显式通道 —— ``state.token_used``（预算口径）与
        run 级 ``TokenLedger``（按 ``state.run_id`` 查找）；不再维护类级全局计数器。
        W5（Q2）：模型名桶 + 职责桶由账本提供（双轨成本报告）。
        审计 F06：区分「provider 缺 usage」（安全跳过）与「业务记账失败」——
        `emit_usage` 在 `DR_USAGE_STRICT=true` 下抛出的 `UsageSinkError` 绝不吞，
        按契约向调用方传播。
        R05：带 ``reservation`` 时按真实 total 结算预占；缺 usage / 字段异常时按预占
        保守计入（不释放），保证预算硬边界。
        """
        u = getattr(resp, "usage", None)
        if u is None:
            settle_call(reservation)
            return
        try:
            total = int(getattr(u, "total_tokens", 0) or 0)
            inp = int(getattr(u, "prompt_tokens", 0) or 0)
            out = int(getattr(u, "completion_tokens", 0) or 0)
        except Exception:  # noqa: BLE001 —— provider usage 字段形态异常：按「无 usage」处理
            settle_call(reservation)
            return
        # 2026-11：run 级账本（由 run_id 从 runtime 注册表查找）。运行期统计以账本为准，
        # 不再把「这次 run 用了多少」押在类级全局计数器上（并发 run 会互相污染）。
        ledger = None
        if state is not None:
            from research_engine.runtime.ledger import ledger_for

            ledger = ledger_for(getattr(state, "run_id", ""))
        if ledger is not None:
            ledger.add(model=self.model, role=self.role, input_tokens=inp,
                       output_tokens=out, total_tokens=total)
        if reservation is not None:
            settle_call(reservation, total)
        elif state is not None:
            add_charged_usage(state, total)
        # P1-4 / F06：逐调用记账（无 sink 时 no-op；strict 失败由 emit_usage 上抛）
        emit_usage(UsageRecord(
            kind="llm", provider="dashscope", model=self.model, role=self.role,
            input_tokens=inp, output_tokens=out, total_tokens=total,
            request_id=getattr(resp, "id", None),
            # 2026-11：显式 run_id ⇒ 优先走注册表 sink（无注册时自动回退 contextvar）
        ), run_id=getattr(state, "run_id", "") if state is not None else "")

    def chat_json(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        state: Optional[Any] = None,
        schema: Optional[type[BaseModel]] = None,
        retries: int = 2,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """结构化输出，返回 JSON 对象（可选 Pydantic 校验 + 自动纠错重试）。

        - 解析或校验失败时，把上次坏输出 + 错误信息追加进 messages 重试，最多 retries 次。
        - schema 传入时用 Pydantic 校验并 model_dump() 成 dict；不传则只保证是合法 JSON。
        - state 照常累加 token 用量（Q6-B）；新增参数带默认值，现有调用零改动。
        - W5（Q4）：timeout 透传（judge 类调用设 60s）。
        """
        attempt = 0
        while True:
            content = self.chat(
                messages,
                temperature=temperature,
                response_format={"type": "json_object"},
                state=state,
                timeout=timeout,
            )
            try:
                obj = self._parse_json(content)
                if schema is not None:
                    obj = schema.model_validate(obj).model_dump()
                return obj
            except (json.JSONDecodeError, ValidationError) as exc:
                attempt += 1
                if attempt > retries:
                    raise
                messages = self._with_correction(messages, content, exc)

    @staticmethod
    def _parse_json(content: str) -> Any:
        """剥掉 ```json 代码块包裹后解析 JSON。"""
        text = content.strip()
        if text.startswith("```"):
            text = text[3:].strip()
            if text.lower().startswith("json"):
                text = text[4:].strip()
        if text.endswith("```"):
            text = text[:-3].strip()
        return json.loads(text)

    @staticmethod
    def _with_correction(
        messages: List[Dict[str, str]], bad_content: str, exc: Exception
    ) -> List[Dict[str, str]]:
        """把坏输出 + 错误反馈追加进 messages，用于纠错重试。"""
        msgs = list(messages)
        msgs.append({"role": "assistant", "content": bad_content[:800]})
        msgs.append(
            {
                "role": "user",
                "content": f"你上一次的输出不是合法 JSON（错误：{exc}）。"
                "请只输出一个合法的 JSON 对象，不要包含解释或 markdown 代码块。",
            }
        )
        return msgs


def build_messages(system: str, user: str) -> List[Dict[str, str]]:
    """构造标准 messages。"""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
