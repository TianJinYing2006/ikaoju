"""Passkey（WebAuthn）领域服务（2026-11 后端按域拆分第八刀的服务层）。

职责：把 Passkey 的**可用性闸 / 视图序列化 / 限流**从 `main.py` 收敛到独立领域模块，
路由层继续留在 `main.py`（后续两刀分别把管理路由、登录认证路由迁到
`web/backend/api/webauthn.py`）。

约束：
- 本模块不 import `main`；限流器、审计、时间格式化、client_key 均参数注入；
- 依赖 `web/backend/passkeys.py`（WebAuthn 库薄封装）做协议层细节，本模块只做业务闸。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from . import passkeys
from .errors import http_error


def require_ready() -> None:
    """Passkey 端点前置：未配置 RP ID/ORIGIN 即 503（fail-closed）。"""
    if not passkeys.enabled():
        raise http_error("webauthn_unavailable", "Passkey 未启用（缺少 DR_WEBAUTHN_RP_ID / ORIGIN）")


def passkey_view(row: dict[str, Any], *, format_dt: Callable[[Any], Optional[str]]) -> dict:
    """凭据行 → 对外视图（不返回公钥；credential_id base64url 化）。"""
    return {
        "credential_id": passkeys.credential_id_b64(bytes(row["credential_id"])),
        "name": row.get("name") or "未命名通行密钥",
        "aaguid": row.get("aaguid"),
        "transports": list(row.get("transports") or []),
        "backup_state": bool(row.get("backup_state")),
        "device_type": row.get("device_type"),
        "created_at": format_dt(row.get("created_at")),
        "last_used_at": format_dt(row.get("last_used_at")),
    }


def check_manage_rate_limit(*, rate_limiter: Any, audit: Callable[..., None],
                            user: dict, request: Any) -> None:
    """P0-2：Passkey 管理（注册/删除）按用户限流，防挑战刷取与枚举。"""
    if not rate_limiter.allow(f"passkey-manage:{user['user_id']}"):
        audit("passkey_manage_rate_limited", request=request, actor_user_id=user["user_id"])
        raise http_error("rate_limited", "Passkey 操作过于频繁，稍后再试")


def check_login_rate_limit(*, login_limiter: Any, account_limiter: Any,
                           audit: Callable[..., None], client_key: Callable[[Any], str],
                           request: Any, email_hash: Optional[str] = None) -> None:
    """Passkey 登录入口限流：IP + 可选账号哈希双维度（防枚举/撞库）。"""
    allowed = login_limiter.allow(f"passkey:{client_key(request)}")
    if email_hash is not None:
        allowed = account_limiter.allow(f"passkey-acct:{email_hash}") and allowed
    if not allowed:
        audit("passkey_rate_limited", request=request,
              detail={"email_hash": email_hash} if email_hash else None)
        raise http_error("rate_limited", "Passkey 请求过于频繁，稍后再试")
