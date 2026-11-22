"""报告分享公开读取路由（2026-11 后端按域拆分第十七刀：列表 / 免登录读取 / SPA 壳）。

拆分原则（`docs/plan/2026-11-execution.md` §3 / `web/backend/api/__init__.py`）：
- 分享闸、只读正文链、输出 gate 仍由 `main.py` 提供（与 share_manage 共用）；
- 本模块不 import `main`；前端产物路径以 callable 注入。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from ..auth import token_hash
from ..errors import http_error


def build_share_public_router(
    *,
    get_store: Callable[[], Any],
    store_call: Callable[..., Any],
    require_user: Callable[[Request], Optional[str]],
    client_key: Callable[[Request], str],
    share_limiter: Callable[[], Any],
    audit: Callable[..., None],
    share_enabled_or_404: Callable[[], None],
    share_markdown: Callable[[str], Optional[str]],
    enforce_output_policy: Callable[[str], None],
    frontend_index: Callable[[], Path],
) -> APIRouter:
    """构造分享公开读取路由；依赖全部由组合根注入。"""
    router = APIRouter()

    @router.get("/api/shares")
    def list_my_shares(request: Request) -> dict:
        """当前用户的活跃分享列表（需求 26 集中管理页）。

        注意：token 明文不可恢复（只存 hash）——管理页只提供状态与撤销；
        重新生成请到对应报告/历史预览的「分享」入口。
        """
        share_enabled_or_404()
        user_id = require_user(request)
        store = get_store()
        if store is None:
            raise http_error("persistence_unavailable", "分享需要任务库（DR_DATABASE_URL）")
        rows = (store_call(store.list_report_shares_for_user, user_id) if user_id
                else store_call(store.list_report_shares, active_only=True))
        return {"shares": [{
            "share_id": row["share_id"],
            "run_id": row["run_id"],
            "topic": row.get("topic"),
            "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
            "permanent": row.get("expires_at") is None,
            "expires_at": row["expires_at"].isoformat() if row.get("expires_at") else None,
            "last_accessed_at": (row["last_accessed_at"].isoformat()
                                 if row.get("last_accessed_at") else None),
            "access_count": row.get("access_count", 0),
        } for row in rows]}

    @router.get("/api/share/{token}")
    def get_shared_report(token: str, request: Request) -> Response:
        """免登录只读分享：统一 404（不存在/撤销/过期/审核未过/无报告）；限流防枚举。"""
        share_enabled_or_404()
        if not token or len(token) > 128:
            raise http_error("share_not_found", "链接无效或已过期")
        if not share_limiter().allow(f"share:{client_key(request)}"):
            raise http_error("rate_limited", "请求过于频繁，稍后再试")
        store = get_store()
        if store is None:
            raise http_error("share_not_found", "链接无效或已过期")
        row = store_call(store.resolve_report_share, token_hash(token))
        if row is None:
            raise http_error("share_not_found", "链接无效或已过期")
        try:
            enforce_output_policy(row["run_id"])
        except Exception:  # noqa: BLE001 —— 审核未过一律并进统一 404
            raise http_error("share_not_found", "链接无效或已过期") from None
        markdown = share_markdown(row["run_id"])
        if markdown is None:
            raise http_error("share_not_found", "链接无效或已过期")
        store_call(store.touch_report_share, token_hash(token))
        audit("share_viewed", request=request, target_id=row["share_id"],
              detail={"run_id": row["run_id"]})
        return JSONResponse(
            {"topic": row.get("topic") or "研究报告", "markdown": markdown,
             "permanent": row.get("expires_at") is None,
             "expires_at": row["expires_at"].isoformat() if row.get("expires_at") else None},
            headers={
                "Cache-Control": "private, no-store",
                "X-Robots-Tag": "noindex, nofollow, noarchive",
                "Referrer-Policy": "no-referrer",
            },
        )

    @router.get("/s/{token}")
    def share_page_shell(token: str) -> Response:
        """SPA 分享页壳（需求 26）：只回 index.html；数据走 /api/share/{token}（统一 404）。

        #337：壳页也必须带 noindex —— 爬虫只取 HTML，看不到 `/api/share` 的响应头；
        否则分享 URL（bearer capability）可能被搜索引擎收录。robots.txt 另有 `/s/` 禁令。
        """
        index = frontend_index()
        if not index.is_file():
            raise http_error("share_not_found", "链接无效或已过期")
        return FileResponse(index, headers={
            "X-Robots-Tag": "noindex, nofollow, noarchive",
        })

    return router
