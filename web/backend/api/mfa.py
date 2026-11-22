"""MFA 域路由（2026-11 后端按域拆分第六/七刀：status / totp begin / confirm / disable / verify）。

拆分原则（`docs/plan/2026-11-execution.md` §3 / `web/backend/api/__init__.py`）：
- 本层只做 HTTP 映射与序列化；TOTP 判定/恢复码/step-up 轮换在 `web/backend/mfa.py`，
  会话判定、限流、审计、任务库调用仍由 `main.py` 注入；
- 本模块不 import `main`，依赖全部由组合根以 getter/callable 注入。
"""
from __future__ import annotations

import uuid
from typing import Any, Callable, Optional

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from .. import secretbox
from ..auth import device_key, is_new_device, verify_password
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


class MfaDisableRequest(BaseModel):
    password: str = Field(..., min_length=1, max_length=200)
    code: str = Field("", max_length=32)


def build_mfa_router(
    *,
    get_store: Callable[[], Any],
    store_call: Callable[..., Any],
    require_session_user: Callable[[Request], dict],
    pending_session_user: Callable[[Request], Optional[dict]],
    public_user: Callable[[dict], dict],
    client_key: Callable[[Request], str],
    check_csrf: Callable[[Request], None],
    audit: Callable[..., None],
    format_dt: Callable[[Any], Optional[str]],
    require_mfa_ready: Callable[[], None],
    verify_totp_factor: Callable[[dict, str], str],
    verify_mfa_factor: Callable[[str, dict, str], str],
    enroll_limiter: Callable[[], Any],
    verify_limiter: Callable[[], Any],
    notify_new_login: Callable[[dict, Request], None],
    rotate_session_after_step_up: Callable[[dict, Request, Response], None],
) -> APIRouter:
    """构造 MFA 域路由；依赖全部由组合根注入。"""
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

    @router.post("/api/auth/mfa/disable")
    def mfa_disable(req: MfaDisableRequest, request: Request) -> dict:
        """关闭 MFA（需密码 + TOTP/恢复码；恢复码路径保证「密钥丢失」时仍可自救）。"""
        check_csrf(request)
        user = require_session_user(request)
        if not enroll_limiter().allow(f"mfa-enroll:{user['user_id']}"):
            audit("mfa_enroll_rate_limited", request=request, actor_user_id=user["user_id"])
            raise http_error("rate_limited", "MFA 操作过于频繁，稍后再试")
        if not verify_password(user["password_hash"], req.password):
            audit("mfa_disable_failed", request=request, actor_user_id=user["user_id"],
                  detail={"reason": "bad_password"})
            raise http_error("invalid_credentials", "密码不正确")
        store = get_store()
        factor = store_call(store.get_mfa_factor, user["user_id"])
        status = (TOTP_OK if factor is None
                  else verify_mfa_factor(user["user_id"], factor, req.code))
        if status != TOTP_OK:
            audit("mfa_disable_failed", request=request, actor_user_id=user["user_id"],
                  detail={"reason": "replay" if status == TOTP_REPLAY else "bad_code"})
            raise http_error("mfa_code_invalid", "验证码或恢复码不正确")
        store_call(store.delete_mfa_factors, user["user_id"])
        store_call(store.replace_recovery_codes, user["user_id"], [])
        audit("mfa_disabled", request=request, actor_user_id=user["user_id"])
        return {"ok": True}

    @router.post("/api/auth/mfa/verify")
    def mfa_verify(req: MfaCodeRequest, request: Request, response: Response) -> dict:
        """登录第二步（step-up）：半程会话 + TOTP/恢复码 ⇒ 标记该会话已完成第二步。

        需求 42：第二步成功后**轮换会话 token**（与登录一致），旧半程 token 立即失效，
        防会话固定（攻击者预置的半程 token 不能借受害者的验证码「转正」）。
        """
        check_csrf(request)
        user = pending_session_user(request)
        if user is None:
            raise http_error("unauthenticated", "请先登录")
        if user.get("session_mfa_verified", True):
            return {"user": public_user(user)}   # 幂等：已完成第二步
        store = get_store()
        factor = store_call(store.get_mfa_factor, user["user_id"])
        if factor is None:
            # 因子已在别处移除（如已关闭 MFA）：直接完成，避免把自己锁死
            store_call(store.set_session_mfa_verified, user["session_token_hash"], True)
            rotate_session_after_step_up(user, request, response)
            return {"user": public_user(user)}
        if not verify_limiter().allow(f"mfa-verify:{user['user_id']}"):
            audit("mfa_verify_rate_limited", request=request, actor_user_id=user["user_id"])
            raise http_error("rate_limited", "验证码尝试过于频繁，稍后再试")
        status = verify_mfa_factor(user["user_id"], factor, req.code)
        if status != TOTP_OK:
            audit("mfa_verify_replay" if status == TOTP_REPLAY else "mfa_verify_failed",
                  request=request, actor_user_id=user["user_id"])
            raise http_error("mfa_code_invalid", "验证码或恢复码不正确")
        store_call(store.set_session_mfa_verified, user["session_token_hash"], True)
        audit("mfa_verified", request=request, actor_user_id=user["user_id"])
        # 第二步完成后才判「新设备」：此时须排除本半程会话
        rows = [row for row in store_call(store.list_sessions, user["user_id"])
                if row.get("token_hash") != user["session_token_hash"]]
        if is_new_device([device_key(row.get("user_agent"), row.get("ip")) for row in rows],
                         request.headers.get("user-agent"), client_key(request)):
            notify_new_login(user, request)
        # 需求 42：轮换 token 放在新设备判定**之后**（否则新会话会把自己算进已知设备）
        rotate_session_after_step_up(user, request, response)
        return {"user": public_user(user)}

    return router
