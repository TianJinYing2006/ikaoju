"""账号凭据域路由（2026-11 后端按域拆分第四刀：注册 / 登录 / 登出）。

拆分原则（`docs/plan/2026-11-execution.md` §3 / `web/backend/api/__init__.py`）：
- 本层只做 HTTP 映射（请求模型 / 状态码 / 序列化）；注册开关、限流器、会话 Cookie、
  审计、CSRF 等仍由 `main.py` 提供并以 callable 注入，本模块不 import `main`；
- 纯函数（邮箱规范化/校验、密码校验、设备指纹、legal 版本）直接 import canonical 模块。
"""
from __future__ import annotations

import hashlib
import uuid
from typing import Any, Callable, Optional

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from .. import secretbox
from ..auth import (
    MIN_PASSWORD_LENGTH,
    device_key,
    hash_password,
    is_new_device,
    is_valid_email,
    normalize_email,
    token_hash,
    verify_password_or_dummy,
)
from ..errors import http_error, safe_detail
from .meta import LEGAL_DIR, legal_version


class RegisterRequest(BaseModel):
    email: str = Field(..., max_length=254)
    password: str = Field(..., min_length=MIN_PASSWORD_LENGTH, max_length=200)
    agree_terms: bool = Field(False, description="需求 25：必须勾选同意《用户协议》与《隐私政策》")


class LoginRequest(BaseModel):
    email: str = Field(..., max_length=254)
    password: str = Field(..., max_length=200)


def build_auth_credentials_router(
    *,
    get_store: Callable[[], Any],
    store_call: Callable[..., Any],
    audit: Callable[..., None],
    public_user: Callable[[dict], dict],
    client_key: Callable[[Request], str],
    login_limiter: Callable[[], Any],
    login_account_limiter: Callable[[], Any],
    registration_enabled: Callable[[], bool],
    registration_daily_limit: Callable[[], int],
    email_domain_blocked: Callable[[str], bool],
    day_start_utc: Callable[[], Any],
    reject_weak_password: Callable[..., None],
    set_session_cookies: Callable[..., None],
    clear_session_cookies: Callable[[Response], None],
    session_user: Callable[[Request], Optional[dict]],
    session_cookie_name: Callable[[], str],
    notify_new_login: Callable[[dict, Request], None],
    check_csrf: Callable[[Request], None],
) -> APIRouter:
    """构造账号凭据域路由；依赖全部由组合根注入（行为与 `main.py` 原实现一致）。"""
    router = APIRouter()

    @router.post("/api/auth/register")
    def auth_register(req: RegisterRequest, request: Request, response: Response) -> dict:
        """开放注册：邮箱 + 密码 + 同意条款；成功后自动登录。"""
        if not login_limiter().allow(f"register:{client_key(request)}"):
            raise http_error("rate_limited", "注册请求过于频繁，稍后再试")
        store = get_store()
        if store is None:
            raise http_error("persistence_unavailable", "账号功能需要任务库（DR_DATABASE_URL）")
        if not registration_enabled():
            audit("register_blocked", request=request, detail={"reason": "disabled"})
            raise http_error("registration_disabled", "注册通道已关闭，请稍后再试")
        email = normalize_email(req.email)
        if not is_valid_email(email):
            raise http_error("invalid_request", "邮箱格式不合法")
        if not req.agree_terms:
            raise http_error("invalid_request", "请先阅读并同意《用户协议》与《隐私政策》")
        reject_weak_password(req.password, email=email)
        if email_domain_blocked(email):
            audit("register_blocked", request=request, detail={"reason": "email_domain"})
            raise http_error("email_domain_blocked", "该邮箱域名不支持注册，请使用常用邮箱")
        daily_limit = registration_daily_limit()
        if daily_limit > 0:
            created_today = store_call(store.count_users_since, day_start_utc())
            if created_today >= daily_limit:
                audit("register_blocked", request=request, detail={"reason": "daily_limit"})
                raise http_error("registration_limit_reached", "今日注册名额已满，请明天再试")
        user_id = uuid.uuid4().hex[:12]
        try:
            user = store.create_user(user_id, email, hash_password(req.password))
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "sqlstate", None) == "23505":
                raise http_error("email_taken", "该邮箱已注册") from exc
            raise http_error(
                "persistence_unavailable",
                safe_detail("注册失败", exc),
            ) from exc
        store_call(store.touch_last_login, user["user_id"])
        set_session_cookies(response, user["user_id"], request)
        # 需求 25：注册同意留档（PIPL 可举证；版本 = 文档内容 hash）
        consent_versions = {
            "terms": legal_version(LEGAL_DIR / "terms-of-service.md"),
            "privacy": legal_version(LEGAL_DIR / "privacy-policy.md"),
        }
        store_call(store.save_user_consents, user["user_id"],
                   [{"doc_type": doc_type, "version": version}
                    for doc_type, version in consent_versions.items()],
                   ip_hash=hashlib.sha256(client_key(request).encode("utf-8")).hexdigest()[:16])
        audit("register_success", request=request, actor_user_id=user["user_id"],
              detail={"consents": consent_versions})
        return {"user": public_user(user)}

    @router.post("/api/auth/login")
    def auth_login(req: LoginRequest, request: Request, response: Response) -> dict:
        """邮箱 + 密码登录；失败统一 401（不区分「用户不存在 / 密码错 / 已封禁」）。

        P1-2：限流双维度 —— IP（`DR_LOGIN_RATE_PER_MINUTE`）与 账号
        （`DR_LOGIN_ACCOUNT_RATE_PER_MINUTE`，邮箱哈希作键，防定向撞库）。
        """
        email = normalize_email(req.email)
        email_hash = hashlib.sha256(email.encode("utf-8")).hexdigest()[:16]
        if (not login_limiter().allow(f"login:{client_key(request)}")
                or not login_account_limiter().allow(f"login-acct:{email_hash}")):
            audit("login_rate_limited", request=request, detail={"email_hash": email_hash})
            raise http_error("rate_limited", "登录请求过于频繁，稍后再试")
        store = get_store()
        if store is None:
            raise http_error("persistence_unavailable", "账号功能需要任务库（DR_DATABASE_URL）")
        user = store_call(store.get_user_by_email, email)
        # 无论用户是否存在/是否封禁都做一次 Argon2 校验，避免响应时间泄露邮箱是否注册
        password_ok = verify_password_or_dummy(
            user["password_hash"] if user is not None else None, req.password)
        if user is None or user["status"] != "active" or not password_ok:
            audit("login_failed", request=request, detail={"email_hash": email_hash})
            raise http_error("invalid_credentials", "邮箱或密码不正确")
        store_call(store.touch_last_login, user["user_id"])
        # P1-1b：先看「该用户既有活跃会话」判断是否新设备，再建本会话（否则自己会被算进去）
        prior = store_call(store.list_sessions, user["user_id"])
        new_device = is_new_device(
            [device_key(row.get("user_agent"), row.get("ip")) for row in prior],
            request.headers.get("user-agent"), client_key(request))
        # P1-3：MFA 用户先发「半程会话」，第二步验证通过才算登录完成。
        # fail-closed：有因子但服务端密钥不可用 ⇒ 拒绝（防「删密钥即绕过第二步」）。
        mfa_factor = store_call(store.get_mfa_factor, user["user_id"])
        if mfa_factor is not None and not secretbox.configured():
            audit("mfa_unavailable", request=request, actor_user_id=user["user_id"])
            raise http_error("mfa_unavailable", "MFA 暂不可用（服务端密钥未配置），请联系管理员")
        set_session_cookies(response, user["user_id"], request,
                            mfa_verified=(mfa_factor is None))
        audit("login_success", request=request, actor_user_id=user["user_id"])
        if mfa_factor is not None:
            audit("mfa_challenge_issued", request=request, actor_user_id=user["user_id"])
            return {"mfa_required": True}
        if new_device:
            notify_new_login(user, request)
        return {"user": public_user(user)}

    @router.post("/api/auth/logout")
    def auth_logout(request: Request, response: Response) -> dict:
        # 需求 42：登出是唯一「有会话却无 CSRF」的写路由，补齐双提交校验（防强制登出）
        check_csrf(request)
        user = session_user(request)
        token = request.cookies.get(session_cookie_name())
        store = get_store()
        if token and store is not None:
            store_call(store.revoke_session, token_hash(token))
        if user is not None:
            audit("logout", request=request, actor_user_id=user["user_id"])
        clear_session_cookies(response)
        return {"ok": True}

    return router
