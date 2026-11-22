"""报告分享管理路由（2026-11 后端按域拆分第十六刀：创建/查询/撤销 run 分享）。

拆分原则（`docs/plan/2026-11-execution.md` §3 / `web/backend/api/__init__.py`）：
- 分享闸/读取链/状态序列化仍由 `main.py` 提供（后续刀随公开读取一并迁移）；
- 本模块不 import `main`，依赖全部由组合根注入。
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, Callable

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..auth import new_token, token_hash
from ..errors import http_error


class ShareCreateRequest(BaseModel):
    """expires_days：1/7/30 天；0 = 永不过期（显式选择，前端二次确认）。"""

    expires_days: int = Field(7, ge=0, le=30)


def build_share_manage_router(
    *,
    get_store: Callable[[], Any],
    store_call: Callable[..., Any],
    authorize_run: Callable[[Request, str], Any],
    check_csrf: Callable[[Request], None],
    audit: Callable[..., None],
    enforce_output_policy: Callable[[str], None],
    share_enabled_or_404: Callable[[], None],
    share_markdown: Callable[[str], Any],
    share_state: Callable[[dict], dict],
    utcnow: Callable[[], Any],
) -> APIRouter:
    """构造分享管理路由；依赖全部由组合根注入。"""
    router = APIRouter()

    @router.post("/api/runs/{run_id}/share")
    def create_run_share(run_id: str, req: ShareCreateRequest, request: Request) -> dict:
        """创建/重建报告只读分享：一 run 单活跃链接（重建即撤销旧的）；token 明文只回一次。"""
        share_enabled_or_404()
        owner = authorize_run(request, run_id) or ""
        check_csrf(request)
        enforce_output_policy(run_id)
        store = get_store()
        if store is None:
            raise http_error("persistence_unavailable", "分享需要任务库（DR_DATABASE_URL）")
        if share_markdown(run_id) is None:
            raise http_error("report_unavailable", "本次运行没有产出报告")
        token = new_token()
        share_id = uuid.uuid4().hex[:12]
        expires_at = (None if req.expires_days == 0
                      else utcnow() + timedelta(days=req.expires_days))
        store_call(store.create_report_share, share_id, token_hash(token), run_id,
                   owner, expires_at)
        audit("share_created", request=request, actor_user_id=owner or None,
              target_id=share_id,
              detail={"run_id": run_id, "expires_days": req.expires_days})
        return {"ok": True, "share_id": share_id, "url": f"/s/{token}",
                "permanent": expires_at is None,
                "expires_at": expires_at.isoformat() if expires_at else None}

    @router.get("/api/runs/{run_id}/share")
    def get_run_share(run_id: str, request: Request) -> dict:
        """当前活跃分享状态（不含 token —— 明文不可恢复；重建即轮换）。"""
        share_enabled_or_404()
        authorize_run(request, run_id)
        store = get_store()
        if store is None:
            raise http_error("persistence_unavailable", "分享需要任务库（DR_DATABASE_URL）")
        row = store_call(store.get_active_report_share, run_id)
        if row is None:
            return {"active": False}
        return {"active": True, **share_state(row)}

    @router.delete("/api/runs/{run_id}/share")
    def revoke_run_share(run_id: str, request: Request) -> dict:
        """撤销该 run 的活跃分享（立即失效）。"""
        share_enabled_or_404()
        owner = authorize_run(request, run_id)
        check_csrf(request)
        store = get_store()
        if store is None:
            raise http_error("persistence_unavailable", "分享需要任务库（DR_DATABASE_URL）")
        revoked = store_call(store.revoke_report_share, run_id)
        if revoked:
            audit("share_revoked", request=request, actor_user_id=owner, target_id=run_id)
        return {"ok": True, "revoked": revoked}

    return router
