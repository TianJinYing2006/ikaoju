"""MFA 读/绑定路由（2026-11 后端按域拆分第六刀：status / totp begin / totp confirm）。

拆分原则（`docs/plan/2026-11-execution.md` §3 / `web/backend/api/__init__.py`）：
- 本层只做 HTTP 映射与序列化；TOTP 判定/恢复码/step-up 轮换在 `web/backend/mfa.py`，
  会话判定、限流、审计、任务库调用仍由 `main.py` 注入；
- `MfaCodeRequest` 由本模块定义并被 `main.py` 的 verify 路由复用（第七刀迁 verify 时移除）。
"""
from __future__ import annotations

import uuid
from typing import Any, Callable, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from .. import secretbox
from ..auth import verify_password
from ..errors import http_error
from ..mfa import MFA_ISSUER, TOTP_OK, TOTP_REPLAY
from ..totp import (
    encode_secret as totp_encode_secret,
)
from ..totp import (
    hash_recovery_code,
    new_recovery_codes,
    new_totp_secret,
)
from ..totp import (
    provisioning_uri as totp_provisioning_uri,
)


class MfaBeginRequest(BaseModel):
    """P0-2c：开始绑定 TOTP 前必须重新输入当前密码（防被劫持会话绑定攻击者因子）。"""

    current_password: str = Field(..., min_length=1, max_length=200)


class MfaCodeRequest(BaseModel):
    code: str = Field(..., min_length=4, max_length=32)


def build_mfa_read_router(
    *,
    get_store: Callable[[], Any],
    store_call: Callable[..., Any],
    require_session_user: Callable[[Request], dict],
    check_csrf: Callable[[Request], None],
    audit: Callable[..., None],
    format_dt: Callable[[Any], Optional[str]],
    require_mfa_ready: Callable[[], None],
    verify_totp_factor: Callable[[dict, str], str],
    enroll_limiter: Callable[[], Any],
) -> APIRouter:
    """构造 MFA 读/绑定路由；依赖全部由组合根注入。"""
    router = APIRouter()

    @router.get("/api/auth/mfa")
    def mfa_status(request: Request) -> dict:
        """当前用户的 MFA 状态（前端「会话与安全」页用）。"""
        user = require_session_user(request)
        store = get_store()
        factor = store_call(store.get_mfa_factor, user["user_id"])
        return {
            "available": secretbox.configured(),
            "enabled": factor is not None,
            "type": factor["type"] if factor else None,
            "confirmed_at": format_dt(factor["confirmed_at"]) if factor else None,
            "recovery_codes_remaining": (
                store_call(store.count_unused_recovery_codes, user["user_id"]) if factor else 0),
        }

    @router.post("/api/auth/mfa/totp/begin")
    def mfa_totp_begin(req: MfaBeginRequest, request: Request) -> dict:
        """开始绑定：生成新 secret（**仅本响应出现一次**）并暂存为未确认因子。

        P0-2c：与 Passkey 添加一致，须重新输入当前密码 —— 仅凭会话 Cookie 不得绑定
        第二因子（否则被盗会话可给账号绑定攻击者的 TOTP 并取走恢复码，受害者自锁）。
        """
        check_csrf(request)
        user = require_session_user(request)
        require_mfa_ready()
        if not enroll_limiter().allow(f"mfa-enroll:{user['user_id']}"):
            audit("mfa_enroll_rate_limited", request=request, actor_user_id=user["user_id"])
            raise http_error("rate_limited", "MFA 操作过于频繁，稍后再试")
        if not verify_password(user["password_hash"], req.current_password):
            audit("mfa_enroll_failed", request=request, actor_user_id=user["user_id"],
                  detail={"reason": "bad_password"})
            raise http_error("invalid_credentials", "密码不正确")
        store = get_store()
        if store_call(store.get_mfa_factor, user["user_id"]) is not None:
            raise http_error("mfa_already_enabled", "已启用 MFA；如需换绑请先关闭")
        secret = new_totp_secret()
        factor_id = uuid.uuid4().hex[:12]
        store_call(store.upsert_pending_mfa_factor, factor_id, user["user_id"],
                   secretbox.encrypt(secret))
        audit("mfa_enroll_started", request=request, actor_user_id=user["user_id"])
        encoded = totp_encode_secret(secret)
        return {"secret": encoded,
                "otpauth_uri": totp_provisioning_uri(encoded, account=user["email"],
                                                     issuer=MFA_ISSUER)}

    @router.post("/api/auth/mfa/totp/confirm")
    def mfa_totp_confirm(req: MfaCodeRequest, request: Request) -> dict:
        """确认绑定：校验一次 TOTP ⇒ 置 confirmed + 生成恢复码（明文**仅此一次**返回）。"""
        check_csrf(request)
        user = require_session_user(request)
        require_mfa_ready()
        if not enroll_limiter().allow(f"mfa-enroll:{user['user_id']}"):
            audit("mfa_enroll_rate_limited", request=request, actor_user_id=user["user_id"])
            raise http_error("rate_limited", "MFA 操作过于频繁，稍后再试")
        store = get_store()
        pending = store_call(store.get_mfa_factor, user["user_id"], confirmed=False)
        if pending is None:
            raise http_error("mfa_not_started", "请先开始绑定（未找到待确认的因子）")
        status = verify_totp_factor(pending, req.code)
        if status != TOTP_OK:
            audit("mfa_confirm_failed", request=request, actor_user_id=user["user_id"],
                  detail={"reason": "replay"} if status == TOTP_REPLAY else None)
            raise http_error("mfa_code_invalid", "验证码不正确")
        if not store_call(store.confirm_mfa_factor, user["user_id"], pending["factor_id"]):
            raise http_error("mfa_not_started", "待确认因子已失效，请重新绑定")
        codes = new_recovery_codes()
        store_call(store.replace_recovery_codes, user["user_id"],
                   [hash_recovery_code(code) for code in codes])
        audit("mfa_enabled", request=request, actor_user_id=user["user_id"])
        return {"ok": True, "recovery_codes": codes}

    return router
