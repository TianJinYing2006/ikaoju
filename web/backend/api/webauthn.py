"""Passkey 管理路由（2026-11 后端按域拆分第九刀：status / register begin / register complete）。

拆分原则（`docs/plan/2026-11-execution.md` §3 / `web/backend/api/__init__.py`）：
- 本层只做 HTTP 映射与序列化；WebAuthn 协议细节在 `web/backend/passkeys.py`，
  可用性闸/视图/限流在 `web/backend/webauthn.py` 服务层；
- 会话判定、审计、任务库调用由 `main.py` 以 getter 注入，本模块不 import `main`。
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from .. import passkeys
from .. import webauthn as passkey_service
from ..auth import (
    device_key,
    is_new_device,
    is_valid_email,
    normalize_email,
    verify_password,
)
from ..errors import http_error, safe_detail


class WebAuthnRegisterCompleteRequest(BaseModel):
    challenge_id: str = Field(..., min_length=8, max_length=128)
    credential: dict[str, Any] = Field(default_factory=dict)
    name: str = Field("", max_length=80)


class WebAuthnRegisterBeginRequest(BaseModel):
    """P0-2b：添加 Passkey 前必须重新输入当前密码（防会话劫持后持久化绑定）。"""

    password: str = Field(..., min_length=1, max_length=200)


class WebAuthnAuthenticateBeginRequest(BaseModel):
    email: str = Field("", max_length=254)


class WebAuthnAuthenticateCompleteRequest(BaseModel):
    challenge_id: str = Field(..., min_length=8, max_length=128)
    credential: dict[str, Any] = Field(default_factory=dict)


class WebAuthnDeleteRequest(BaseModel):
    password: str = Field(..., min_length=1, max_length=200)


def build_webauthn_manage_router(
    *,
    get_store: Callable[[], Any],
    store_call: Callable[..., Any],
    require_session_user: Callable[[Request], dict],
    check_csrf: Callable[[Request], None],
    audit: Callable[..., None],
    format_dt: Callable[[Any], Any],
    manage_limiter: Callable[[], Any],
    public_user: Callable[[dict], dict],
    client_key: Callable[[Request], str],
    login_limiter: Callable[[], Any],
    login_account_limiter: Callable[[], Any],
    set_session_cookies: Callable[..., None],
    notify_new_login: Callable[[dict, Request], None],
) -> APIRouter:
    """构造 Passkey 管理路由；依赖全部由组合根注入。"""
    router = APIRouter()

    def _view(row: dict) -> dict:
        return passkey_service.passkey_view(row, format_dt=format_dt)

    def _manage_rate_limit(user: dict, request: Request) -> None:
        passkey_service.check_manage_rate_limit(
            rate_limiter=manage_limiter(), audit=audit, user=user, request=request)

    @router.get("/api/auth/webauthn")
    def webauthn_status(request: Request) -> dict:
        """当前用户的 Passkey 列表（管理面板用；不返回公钥）。"""
        user = require_session_user(request)
        rows = store_call(get_store().list_webauthn_credentials, user["user_id"])
        return {
            "available": passkeys.enabled(),
            "rp_id": passkeys.rp_id(),
            "credentials": [_view(row) for row in rows],
        }

    @router.post("/api/auth/webauthn/register/begin")
    def webauthn_register_begin(req: WebAuthnRegisterBeginRequest, request: Request) -> dict:
        check_csrf(request)
        user = require_session_user(request)
        _manage_rate_limit(user, request)
        passkey_service.require_ready()
        if not verify_password(user["password_hash"], req.password):
            audit("passkey_enroll_auth_failed", request=request, actor_user_id=user["user_id"])
            raise http_error("invalid_credentials", "密码不正确")
        store = get_store()
        existing = store_call(store.list_webauthn_credentials, user["user_id"])
        challenge, options = passkeys.registration_options(
            user["user_id"], user["email"], existing)
        challenge_id = uuid.uuid4().hex
        store_call(store.create_webauthn_challenge, challenge_id, user["user_id"],
                   "register", challenge,
                   datetime.now(UTC) + timedelta(seconds=passkeys.CHALLENGE_TTL_SECONDS))
        audit("passkey_enroll_started", request=request, actor_user_id=user["user_id"])
        return {"challenge_id": challenge_id, "options": options}

    @router.post("/api/auth/webauthn/register/complete")
    def webauthn_register_complete(req: WebAuthnRegisterCompleteRequest,
                                   request: Request) -> dict:
        check_csrf(request)
        user = require_session_user(request)
        _manage_rate_limit(user, request)
        passkey_service.require_ready()
        store = get_store()
        challenge_row = store_call(store.consume_webauthn_challenge,
                                   req.challenge_id, "register")
        if challenge_row is None or challenge_row.get("user_id") != user["user_id"]:
            raise http_error("webauthn_challenge_invalid", "注册挑战无效或已过期")
        try:
            verified = passkeys.verify_registration(
                req.credential, bytes(challenge_row["challenge"]))
        except Exception as exc:  # noqa: BLE001 —— 客户端输入不可信，统一按校验失败处理
            audit("passkey_register_failed", request=request,
                  actor_user_id=user["user_id"],
                  detail={"reason": type(exc).__name__})
            raise http_error("webauthn_registration_invalid", "Passkey 注册校验未通过") from exc
        name = (req.name or "").strip()[:64] or "通行密钥"
        try:
            store.create_webauthn_credential(
                bytes(verified.credential_id), user["user_id"],
                bytes(verified.credential_public_key), int(verified.sign_count),
                transports=passkeys.transports_from_payload(req.credential),
                aaguid=(verified.aaguid or None),
                backup_state=bool(verified.credential_backed_up),
                device_type=passkeys.device_type(verified.credential_device_type),
                name=name,
            )
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "sqlstate", None) == "23505":
                raise http_error("passkey_exists", "该 Passkey 已绑定") from exc
            raise http_error(
                "persistence_unavailable",
                safe_detail("保存 Passkey 失败", exc),
            ) from exc
        audit("passkey_registered", request=request, actor_user_id=user["user_id"],
              detail={"credential_id": passkeys.credential_id_b64(
                  bytes(verified.credential_id))})
        rows = store_call(store.list_webauthn_credentials, user["user_id"])
        row = next((item for item in rows
                    if bytes(item["credential_id"]) == bytes(verified.credential_id)), None)
        return {"ok": True, "credential": _view(row) if row else None}


    def _login_rate_limit(request: Request, email_hash=None) -> None:
        passkey_service.check_login_rate_limit(
            login_limiter=login_limiter(), account_limiter=login_account_limiter(),
            audit=audit, client_key=client_key, request=request, email_hash=email_hash)

    @router.post("/api/auth/webauthn/authenticate/begin")
    def webauthn_authenticate_begin(req: WebAuthnAuthenticateBeginRequest,
                                    request: Request) -> dict:
        """Passkey 登录第一步：邮箱可空（usernameless / 可发现凭据）。"""
        passkey_service.require_ready()
        email = normalize_email(req.email)
        if email and not is_valid_email(email):
            raise http_error("invalid_request", "邮箱格式不合法")
        email_hash = hashlib.sha256(email.encode("utf-8")).hexdigest()[:16] if email else None
        _login_rate_limit(request, email_hash)
        store = get_store()
        user = store_call(store.get_user_by_email, email) if email else None
        if user is not None and user.get("status") != "active":
            user = None
        credentials = (store_call(store.list_webauthn_credentials, user["user_id"])
                       if user is not None else [])
        challenge, options = passkeys.authentication_options(credentials)
        challenge_id = uuid.uuid4().hex
        store_call(store.create_webauthn_challenge, challenge_id,
                   user["user_id"] if user is not None else None,
                   "authenticate", challenge,
                   datetime.now(UTC) + timedelta(seconds=passkeys.CHALLENGE_TTL_SECONDS))
        audit("passkey_login_started", request=request,
              detail={"email_hash": email_hash} if email_hash else None)
        return {"challenge_id": challenge_id, "options": options}

    @router.post("/api/auth/webauthn/authenticate/complete")
    def webauthn_authenticate_complete(req: WebAuthnAuthenticateCompleteRequest,
                                       request: Request, response: Response) -> dict:
        _login_rate_limit(request)
        passkey_service.require_ready()
        store = get_store()
        try:
            credential_id = passkeys.credential_id_from_payload(req.credential)
        except ValueError as exc:
            raise http_error("webauthn_authentication_invalid", "Passkey 凭据无效") from exc
        credential = store_call(store.get_webauthn_credential, credential_id)
        if credential is None:
            audit("passkey_login_failed", request=request, detail={"reason": "not_found"})
            raise http_error("webauthn_authentication_invalid", "Passkey 登录校验未通过")
        user = store_call(store.get_user, credential["user_id"])
        if user is None or user.get("status") != "active":
            audit("passkey_login_failed", request=request, detail={"reason": "inactive"})
            raise http_error("webauthn_authentication_invalid", "Passkey 登录校验未通过")
        challenge_row = store_call(store.consume_webauthn_challenge,
                                   req.challenge_id, "authenticate")
        if challenge_row is None:
            raise http_error("webauthn_challenge_invalid", "登录挑战无效或已过期")
        bound_user_id = challenge_row.get("user_id")
        if bound_user_id is not None and bound_user_id != user["user_id"]:
            audit("passkey_login_failed", request=request, actor_user_id=user["user_id"],
                  detail={"reason": "user_mismatch"})
            raise http_error("webauthn_authentication_invalid", "Passkey 登录校验未通过")
        try:
            verified = passkeys.verify_authentication(
                req.credential, bytes(challenge_row["challenge"]),
                bytes(credential["public_key"]), int(credential["sign_count"]))
        except Exception as exc:  # noqa: BLE001
            audit("passkey_login_failed", request=request, actor_user_id=user["user_id"],
                  detail={"reason": type(exc).__name__})
            raise http_error("webauthn_authentication_invalid", "Passkey 登录校验未通过") from exc
        if bytes(verified.credential_id) != credential_id:
            audit("passkey_login_failed", request=request, actor_user_id=user["user_id"],
                  detail={"reason": "credential_mismatch"})
            raise http_error("webauthn_authentication_invalid", "Passkey 登录校验未通过")
        previous = int(credential["sign_count"])
        new_count = int(verified.new_sign_count)
        if (previous > 0 or new_count > 0) and new_count <= previous:
            # 克隆 / 重放信号：计数器必须严格递增（双方均 =0 表示认证器不提供计数器）
            audit("passkey_clone_suspected", request=request, actor_user_id=user["user_id"],
                  detail={"previous": previous, "new": new_count})
            raise http_error("webauthn_authentication_invalid", "Passkey 计数器异常，已拒绝登录")
        if not store_call(store.bump_webauthn_sign_count, credential_id, previous, new_count):
            audit("passkey_login_failed", request=request, actor_user_id=user["user_id"],
                  detail={"reason": "sign_count_race"})
            raise http_error("webauthn_authentication_invalid", "Passkey 登录校验未通过")
        prior = store_call(store.list_sessions, user["user_id"])
        new_device = is_new_device(
            [device_key(row.get("user_agent"), row.get("ip")) for row in prior],
            request.headers.get("user-agent"), client_key(request))
        store_call(store.touch_last_login, user["user_id"])
        # Passkey 本身是「持有 + 用户验证」的 MFA：登录成功即视为完成第二步。
        set_session_cookies(response, user["user_id"], request, mfa_verified=True)
        audit("login_success", request=request, actor_user_id=user["user_id"],
              detail={"method": "passkey"})
        audit("passkey_login_success", request=request, actor_user_id=user["user_id"],
              detail={"sign_count": new_count,
                      "backup_state": bool(verified.credential_backed_up)})
        if new_device:
            notify_new_login(user, request)
        return {"user": public_user(user)}


    @router.delete("/api/auth/webauthn/{credential_id}")
    def webauthn_delete(credential_id: str, req: WebAuthnDeleteRequest,
                        request: Request) -> dict:
        check_csrf(request)
        user = require_session_user(request)
        _manage_rate_limit(user, request)
        passkey_service.require_ready()
        store = get_store()
        try:
            raw_id = passkeys.credential_id_from_payload({"rawId": credential_id})
        except ValueError as exc:
            raise http_error("passkey_not_found", "Passkey 不存在") from exc
        row = store_call(store.get_webauthn_credential, raw_id)
        if row is None or row.get("user_id") != user["user_id"]:
            raise http_error("passkey_not_found", "Passkey 不存在")
        if not verify_password(user["password_hash"], req.password):
            audit("passkey_delete_failed", request=request, actor_user_id=user["user_id"],
                  detail={"reason": "bad_password"})
            raise http_error("invalid_credentials", "密码不正确")
        remaining = store_call(store.count_webauthn_credentials, user["user_id"]) - 1
        store_call(store.delete_webauthn_credential, user["user_id"], raw_id)
        audit("passkey_deleted", request=request, actor_user_id=user["user_id"],
              detail={"credential_id": passkeys.credential_id_b64(raw_id),
                      "last_credential": remaining <= 0})
        return {"ok": True, "remaining": max(remaining, 0)}

    return router
