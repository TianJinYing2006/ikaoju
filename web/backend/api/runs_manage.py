"""任务管理路由（2026-11 后端按域拆分第十一刀：历史列表 / 重命名）。

拆分原则（`docs/plan/2026-11-execution.md` §3 / `web/backend/api/__init__.py`）：
- 本层只做 HTTP 映射与序列化；任务库访问与归属校验由 `main.py` 注入；
- 本模块不 import `main`；`run_brief` 序列化暂沿用 main 的实现（后续刀一并迁入）。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from ..errors import ApiError, http_error
from ..moderation import MAX_TOPIC_LENGTH
from ..profiles import DEFAULT_PROFILE, resolve_profile


class RenameRunRequest(BaseModel):
    topic: str = Field(..., max_length=MAX_TOPIC_LENGTH)


class StartResponse(BaseModel):
    run_id: str


def build_runs_manage_router(
    *,
    get_store: Callable[[], Any],
    store_call: Callable[..., Any],
    require_user: Callable[[Request], Optional[str]],
    authorize_run: Callable[[Request, str], Optional[str]],
    check_csrf: Callable[[Request], None],
    audit: Callable[..., None],
    run_brief: Callable[[dict], dict],
    open_statuses: Callable[[], Any],
    client_key: Callable[[Request], str],
    submit_limiter: Callable[[], Any],
    check_input_safety: Callable[..., None],
    start_queued: Callable[..., str],
    manager_start: Callable[..., str],
    effective_run_budget: Callable[[Any], float],
    admission_limits: Callable[[float], dict],
    request_fingerprint: Callable[..., str],
    idempotent_existing: Callable[..., Optional[dict]],
    execution_mode: Callable[[], str],
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


    def _owned_row(request: Request, run_id: str) -> tuple[Any, dict]:
        store = get_store()
        if store is None:
            raise http_error("persistence_unavailable", "未配置任务库（DR_DATABASE_URL）")
        owner = authorize_run(request, run_id)
        check_csrf(request)
        row = store_call(store.get_run, run_id)
        if row is None:
            raise http_error("run_id_not_found", f"run_id 不存在：{run_id}")
        return owner, row

    @router.post("/api/runs/{run_id}/pin")
    def pin_run(run_id: str, request: Request) -> dict:
        """需求 22：置顶（幂等）。"""
        owner, _ = _owned_row(request, run_id)
        store_call(get_store().set_pinned, run_id, True, user_id=owner)
        audit("run_pinned", request=request, actor_user_id=owner,
              target_type="run", target_id=run_id)
        return {"ok": True, "run_id": run_id, "pinned": True}

    @router.post("/api/runs/{run_id}/unpin")
    def unpin_run(run_id: str, request: Request) -> dict:
        """需求 22：取消置顶（幂等）。"""
        owner, _ = _owned_row(request, run_id)
        store_call(get_store().set_pinned, run_id, False, user_id=owner)
        audit("run_unpinned", request=request, actor_user_id=owner,
              target_type="run", target_id=run_id)
        return {"ok": True, "run_id": run_id, "pinned": False}

    @router.post("/api/runs/{run_id}/archive")
    def archive_run(run_id: str, request: Request) -> dict:
        """需求 22：软归档（仅影响默认列表可见性；进行中任务 409）。

        需求 36：等待人类输入（AWAITING_*）也算「进行中」，不得归档。
        """
        owner, row = _owned_row(request, run_id)
        if row["status"] in open_statuses():
            raise http_error("run_active", "进行中的任务不能归档。",
                             detail=f"status={row['status']}")
        store_call(get_store().set_archived, run_id, True, user_id=owner)
        audit("run_archived", request=request, actor_user_id=owner,
              target_type="run", target_id=run_id)
        return {"ok": True, "run_id": run_id, "archived": True}

    @router.post("/api/runs/{run_id}/unarchive")
    def unarchive_run(run_id: str, request: Request) -> dict:
        """需求 22：取消归档（幂等）。"""
        owner, _ = _owned_row(request, run_id)
        store_call(get_store().set_archived, run_id, False, user_id=owner)
        audit("run_unarchived", request=request, actor_user_id=owner,
              target_type="run", target_id=run_id)
        return {"ok": True, "run_id": run_id, "archived": False}


    @router.post("/api/runs/{run_id}/retry", response_model=StartResponse)
    def retry_run(run_id: str, request: Request) -> StartResponse:
        """需求 22：失败任务一键重试 —— 复制原 request 快照创建**新 run**（`retry_of` 血缘）。

        幂等键固定 `retry-{原 run_id}`：重复点击返回同一个新 run；配额 / 预算照常走准入事务。
        仅终态失败（FAILED / LOST / TIMED_OUT）可重试；其余 409。
        """
        store = get_store()
        if store is None:
            raise http_error("persistence_unavailable", "未配置任务库（DR_DATABASE_URL）")
        owner = authorize_run(request, run_id)
        check_csrf(request)
        if not submit_limiter().allow(f"submit:{owner or client_key(request)}"):
            raise http_error("rate_limited", "提交过于频繁，稍后再试")
        row = store_call(store.get_run, run_id)
        if row is None:
            raise http_error("run_id_not_found", f"run_id 不存在：{run_id}")
        if row["status"] not in ("FAILED", "LOST", "TIMED_OUT"):
            raise http_error(
                "run_not_retryable",
                "仅失败 / 失联 / 超时的任务可重试",
                detail=f"status={row['status']}",
            )
        payload = row.get("request") or {}
        profile_name = (payload.get("profile") or {}).get("name") or DEFAULT_PROFILE
        profile = resolve_profile(profile_name) or resolve_profile(DEFAULT_PROFILE)
        if profile is None:
            raise http_error("unknown_profile", "档位配置不可用")
        topic = row["topic"]
        instructions = payload.get("instructions") or ""
        idempotency_key = f"retry-{run_id}"
        ignored = payload.get("ignored_overrides") or {}
        fingerprint = request_fingerprint(topic, instructions, profile.name)
        # 2026-11 PR 审阅修复：幂等命中**先于**输入安全闸（与主创建路径同序）。
        existing = idempotent_existing(owner, idempotency_key, fingerprint)
        if existing is not None:
            return StartResponse(run_id=existing["run_id"])
        try:
            # 2026-11 修复：重试也必须过输入安全闸（不得绕过审核/注入预检），降级同样 fail-closed
            check_input_safety(topic=topic, instructions=instructions,
                               user_id=owner, request=request)
            if execution_mode() == "queue":
                new_run_id = start_queued(
                    topic=topic, instructions=instructions, idempotency_key=idempotency_key,
                    user_id=owner, profile=profile, ignored=ignored, fingerprint=fingerprint,
                    retry_of=run_id)
            else:
                budget = effective_run_budget(profile)
                new_run_id = manager_start(
                    topic, instructions, profile,
                    idempotency_key, user_id=owner,
                    budget_limit_cny=budget,
                    ignored_overrides=ignored,
                    admission=admission_limits(budget),
                    request_hash=fingerprint, retry_of=run_id)
        except ApiError as exc:
            raise exc.to_http() from exc
        audit("run_retried", request=request, actor_user_id=owner, target_type="run",
              target_id=run_id, detail={"new_run_id": new_run_id})
        return StartResponse(run_id=new_run_id)

    return router
