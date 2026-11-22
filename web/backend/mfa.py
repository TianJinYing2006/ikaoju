"""MFA（TOTP + 恢复码）领域服务（2026-11 后端按域拆分第五刀的服务层）。

职责：把 MFA 的**判定/轮换逻辑**从 `main.py` 收敛到独立领域模块，路由层继续留在
`main.py`（下一刀再把 5 个 MFA 路由与请求模型搬到 `web/backend/api/mfa.py`）。

约束（与既有实现逐字对齐）：
- 校验 TOTP 时**原子消费**时间步（RFC 6238 §5.2：同一 OTP 不得二次使用，需求 42）；
- 密钥不可用/密文损坏一律 fail-closed（`TOTP_INVALID`）；
- 恢复码仅在 TOTP 未命中且非重放时尝试（重放不再回退恢复码，避免扩大暴力面）；
- 本模块不 import `main`：任务库调用与会话 Cookie 下发经参数注入（组合根绑定）。
"""
from __future__ import annotations

import time
from typing import Any, Callable

from . import secretbox
from .errors import http_error
from .totp import hash_recovery_code, verify_totp_step

#: 认证器 App 中显示的签发方
MFA_ISSUER = "爱考据 iKaoju"

#: TOTP 校验结果（需求 42：防重放需区分「码不对」与「码已被用过」）
TOTP_OK = "ok"
TOTP_INVALID = "invalid"
TOTP_REPLAY = "replay"


def require_mfa_ready() -> None:
    """MFA 端点前置：服务端密钥未配置即 503（fail-closed，不静默降级）。"""
    if not secretbox.configured():
        raise http_error("mfa_unconfigured", "MFA 未启用（服务端缺少 DR_MFA_SECRET_KEY）")


def verify_totp_factor(*, store: Any, store_call: Callable[..., Any], factor: dict,
                       code: str) -> str:
    """校验 TOTP 并**原子消费**时间步；返回 `TOTP_OK` / `TOTP_INVALID` / `TOTP_REPLAY`。

    密钥不可用 / 密文损坏一律 `TOTP_INVALID`（fail-closed）；命中但该时间步已被
    消费 ⇒ `TOTP_REPLAY`（RFC 6238 §5.2：同一 OTP 不得二次使用，需求 42）。
    """
    try:
        secret = secretbox.decrypt(factor["secret_encrypted"])
    except Exception:  # noqa: BLE001
        return TOTP_INVALID
    step = verify_totp_step(secret, code, time.time())
    if step is None:
        return TOTP_INVALID
    if not store_call(store.consume_mfa_factor_step, factor["factor_id"], step):
        return TOTP_REPLAY
    return TOTP_OK


def verify_mfa_factor(*, store: Any, store_call: Callable[..., Any], user_id: str,
                      factor: dict, code: str) -> str:
    """TOTP 优先；否则尝试**恢复码**（单次消费成功才算通过）。返回 TOTP 结果常量。

    恢复码不依赖服务端密钥 ⇒ **认证器丢失**时仍可关闭 MFA（防用户自锁）。
    （注意：若服务端密钥丢失，登录会在第一步即 fail-closed，恢复码无法用于登录 ——
    须由运维恢复 `DR_MFA_SECRET_KEY`。）
    """
    status = verify_totp_factor(store=store, store_call=store_call, factor=factor, code=code)
    if status in (TOTP_OK, TOTP_REPLAY):
        # 重放不再回退恢复码：该码已被 TOTP 匹配过，回退只会扩大暴力面
        return status
    if store_call(store.consume_recovery_code, user_id, hash_recovery_code(code)):
        return TOTP_OK
    return TOTP_INVALID


def rotate_session_after_step_up(*, store: Any, store_call: Callable[..., Any],
                                 set_session_cookies: Callable[..., None],
                                 user: dict, request: Any, response: Any) -> None:
    """需求 42：第二步验证成功后轮换会话 token（吊销旧半程会话并重签 Cookie）。

    登录本身已轮换 token；step-up 沿用同一纪律，避免预置的半程会话在验证后继续有效。
    """
    store_call(store.revoke_session, user["session_token_hash"])
    set_session_cookies(response, user["user_id"], request)
