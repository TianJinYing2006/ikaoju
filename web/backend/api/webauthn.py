"""Passkey 管理路由（2026-11 后端按域拆分第九刀：status / register begin / register complete）。

拆分原则（`docs/plan/2026-11-execution.md` §3 / `web/backend/api/__init__.py`）：
- 本层只做 HTTP 映射与序列化；WebAuthn 协议细节在 `web/backend/passkeys.py`，
  可用性闸/视图/限流在 `web/backend/webauthn.py` 服务层；
- 会话判定、审计、任务库调用由 `main.py` 以 getter 注入，本模块不 import `main`。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from .. import passkeys
from .. import webauthn as passkey_service
from ..auth import verify_password
from ..errors import http_error, safe_detail


class WebAuthnRegisterCompleteRequest(BaseModel):
    challenge_id: str = Field(..., min_length=8, max_length=128)
    credential: dict[str, Any] = Field(default_factory=dict)
    name: str = Field("", max_length=80)


class WebAuthnRegisterBeginRequest(BaseModel):
    """P0-2b：添加 Passkey 前必须重新输入当前密码（防会话劫持后持久化绑定）。"""

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

    return router
