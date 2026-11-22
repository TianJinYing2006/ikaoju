"""会话域路由（2026-11 后端按域拆分第三刀：当前会话 / 活跃会话列表 / 撤销会话）。

拆分原则（`docs/plan/2026-11-execution.md` §3 / `web/backend/api/__init__.py`）：
- 本层只做 HTTP 映射与序列化，不承载任务库 SQL / 会话存储实现；
- 依赖由组合根 `main.py` 以 getter/callable 注入（`build_auth_sessions_router`），
  本模块不 import `main`；会话判定、CSRF、重认证限流、审计、Cookie 清理仍在
  `main.py`（或 canonical auth 模块）里，行为不变。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from ..auth import verify_password
from ..errors import http_error


class RevokeSessionRequest(BaseModel):
    password: Optional[str] = Field(None, max_length=200)


def build_auth_sessions_router(
    *,
    get_store: Callable[[], Any],
    session_user: Callable[[Request], Optional[dict]],
    require_session_user: Callable[[Request], dict],
    public_user: Callable[[dict], dict],
    store_call: Callable[..., Any],
    format_dt: Callable[[Any], Optional[str]],
    check_csrf: Callable[[Request], None],
    reauth_rate_limit: Callable[[dict, Request, str], None],
    audit: Callable[..., None],
    clear_session_cookies: Callable[[Response], None],
) -> APIRouter:
    """构造会话域路由；依赖全部显式注入（组合根保证与既有实现同源）。"""
    router = APIRouter()

    @router.get("/api/auth/session")
    def auth_session(request: Request) -> dict:
        user = session_user(request)
        if user is None:
            raise http_error("unauthenticated", "未登录")
        return {"user": public_user(user)}

    @router.get("/api/auth/sessions")
    def auth_sessions(request: Request) -> dict:
        """本用户活跃会话列表（P1-10）：对外只暴露 `session_id`，不泄露 token 摘要。"""
        user = require_session_user(request)
        current = user.get("session_token_hash")
        rows = store_call(get_store().list_sessions, user["user_id"])
        return {"sessions": [{
            "session_id": row["session_id"],
            "current": row["token_hash"] == current,
            "created_at": format_dt(row["created_at"]),
            "last_seen_at": format_dt(row["last_seen_at"]),
            "ip": row["ip"],
            "user_agent": row["user_agent"],
        } for row in rows]}

    @router.delete("/api/auth/sessions/{session_id}")
    def auth_revoke_session(session_id: str, req: RevokeSessionRequest, request: Request,
                            response: Response) -> dict:
        """终止一个会话（P1-10）；终止**其他**会话需重认证（ASVS 7.5.2）。"""
        user = require_session_user(request)
        check_csrf(request)
        is_current = user.get("session_id") == session_id
        if not is_current:
            # 需求 42：重认证（终止他人会话）按用户限流，密码错误留审计
            reauth_rate_limit(user, request, "session_revoke")
            if not req.password or not verify_password(user["password_hash"], req.password):
                audit("session_revoke_denied", request=request, actor_user_id=user["user_id"],
                      target_id=session_id, detail={"reason": "bad_password"})
                raise http_error("invalid_credentials", "终止其他会话需要输入当前密码")
        if not store_call(get_store().revoke_session_by_id, user["user_id"], session_id):
            raise http_error("invalid_request", "会话不存在")
        audit("session_revoked", request=request, actor_user_id=user["user_id"],
              target_id=session_id, detail={"current": is_current})
        if is_current:
            clear_session_cookies(response)
        return {"ok": True}

    @router.delete("/api/auth/sessions")
    def auth_revoke_other_sessions(req: RevokeSessionRequest, request: Request) -> dict:
        """退出其他所有设备（P1-10）：需重认证；当前会话保留。"""
        user = require_session_user(request)
        check_csrf(request)
        # 需求 42：重认证（退出其他所有设备）按用户限流，密码错误留审计
        reauth_rate_limit(user, request, "sessions_revoke_others")
        if not req.password or not verify_password(user["password_hash"], req.password):
            audit("session_revoke_denied", request=request, actor_user_id=user["user_id"],
                  detail={"scope": "others", "reason": "bad_password"})
            raise http_error("invalid_credentials", "退出其他设备需要输入当前密码")
        revoked = store_call(get_store().revoke_other_sessions, user["user_id"],
                             user["session_token_hash"])
        audit("sessions_revoked_others", request=request, actor_user_id=user["user_id"],
              detail={"revoked": revoked})
        return {"ok": True, "revoked": revoked}

    return router
