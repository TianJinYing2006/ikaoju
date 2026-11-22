"""任务管理路由（2026-11 后端按域拆分第十一刀：历史列表 / 重命名）。

拆分原则（`docs/plan/2026-11-execution.md` §3 / `web/backend/api/__init__.py`）：
- 本层只做 HTTP 映射与序列化；任务库访问与归属校验由 `main.py` 注入；
- 本模块不 import `main`；`run_brief` 序列化暂沿用 main 的实现（后续刀一并迁入）。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from ..errors import http_error
from ..moderation import MAX_TOPIC_LENGTH


class RenameRunRequest(BaseModel):
    topic: str = Field(..., max_length=MAX_TOPIC_LENGTH)


def build_runs_manage_router(
    *,
    get_store: Callable[[], Any],
    store_call: Callable[..., Any],
    require_user: Callable[[Request], Optional[str]],
    authorize_run: Callable[[Request, str], Optional[str]],
    check_csrf: Callable[[Request], None],
    audit: Callable[..., None],
    run_brief: Callable[[dict], dict],
) -> APIRouter:
    """构造任务管理路由；依赖全部由组合根注入。"""
    router = APIRouter()

    @router.get("/api/runs")
    def list_runs(
        request: Request,
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
        status: Optional[str] = Query(None, description="按状态过滤（逗号分隔，如 RUNNING,FAILED）"),
        q: Optional[str] = Query(None, max_length=100, description="关键词（topic 模糊搜索）"),
        archived: bool = Query(False, description="true=只看归档；默认排除归档"),
    ) -> dict:
        """历史任务列表（P2-C / 需求 22）：读任务库；未配置库时结构化 503。

        P4-A：鉴权开启时只返回当前用户的 run。
        需求 22：`q` 关键词搜索 + `archived` 归档过滤 + 置顶优先排序。
        """
        store = get_store()
        if store is None:
            raise http_error(
                "persistence_unavailable",
                "未配置任务库（DR_DATABASE_URL），无法列出历史任务",
            )
        statuses = [item.strip() for item in status.split(",") if item.strip()] if status else None
        keyword = (q or "").strip() or None
        rows = store_call(store.list_runs, user_id=require_user(request),
                          limit=limit, offset=offset, statuses=statuses,
                          q=keyword, archived=archived)
        return {"runs": [run_brief(row) for row in rows], "limit": limit, "offset": offset}

    @router.patch("/api/runs/{run_id}")
    def rename_run(run_id: str, req: RenameRunRequest, request: Request) -> dict:
        """需求 22：重命名（仅本人）。导出文件名随 topic 变化属预期行为，审计留痕。"""
        store = get_store()
        if store is None:
            raise http_error("persistence_unavailable", "未配置任务库（DR_DATABASE_URL）")
        owner = authorize_run(request, run_id)
        check_csrf(request)
        row = store_call(store.get_run, run_id)
        if row is None:
            raise http_error("run_id_not_found", f"run_id 不存在：{run_id}")
        topic = req.topic.strip()
        if not topic:
            raise http_error("empty_topic", "topic 不能为空")
        if not store_call(store.update_topic, run_id, topic, user_id=owner):
            raise http_error("run_id_not_found", f"run_id 不存在：{run_id}")
        audit("run_renamed", request=request, actor_user_id=owner, target_type="run",
              target_id=run_id,
              detail={"old_topic": row["topic"][:200], "new_topic": topic[:200]})
        return {"ok": True, "run_id": run_id, "topic": topic}

    return router
