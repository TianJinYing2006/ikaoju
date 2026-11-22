"""知识库写路由（2026-11 后端按域拆分第十三刀：元数据 / 重分块 / 重新嵌入）。

拆分原则（`docs/plan/2026-11-execution.md` §3 / `web/backend/api/__init__.py`）：
- 本层只做 HTTP 映射与参数校验；归属前置 `_rag_owned_doc` 仍由 `main.py` 注入
  （删除/摄取状态等后续刀共用），任务库与限流由组合根提供；
- 本模块不 import `main`。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..errors import http_error


class RagDocMetaRequest(BaseModel):
    doc_id: str = Field(..., max_length=128)
    display_name: Optional[str] = Field(None, max_length=120)
    tags: Optional[list[str]] = Field(None, max_length=20)


class RagRebuildRequest(BaseModel):
    doc_id: str = Field(..., max_length=128)


def build_rag_write_router(
    *,
    get_store: Callable[[], Any],
    store_call: Callable[..., Any],
    require_user: Callable[[Request], Optional[str]],
    check_csrf: Callable[[Request], None],
    audit: Callable[..., None],
    client_key: Callable[[Request], str],
    upload_limiter: Callable[[], Any],
) -> APIRouter:
    """构造知识库写路由；依赖全部由组合根注入。"""
    router = APIRouter()


    def _owned_doc(request: Request, doc_id: str) -> tuple[Optional[str], dict]:
        """需求 23 写操作前置：任务库 + 归属（非本人 404）+ CSRF + 台账行。"""
        store = get_store()
        if store is None:
            raise http_error("persistence_unavailable", "知识库管理需要任务库（DR_DATABASE_URL）")
        user_id = require_user(request)
        check_csrf(request)
        row = store_call(store.get_rag_doc_for_user, doc_id, user_id)
        if row is None:
            raise http_error("ingestion_not_found", f"文档不存在：{doc_id}")
        return user_id, row
    @router.patch("/api/rag/docs")
    def rag_update_doc(req: RagDocMetaRequest, request: Request) -> dict:
        """重命名 / 标签（需求 23）；`display_name` 为空字符串非法（不传字段 = 不变）。"""
        user_id, _ = _owned_doc(request, req.doc_id)
        display_name = req.display_name.strip() if req.display_name is not None else None
        if display_name == "":
            raise http_error("invalid_request", "display_name 不能为空字符串")
        tags: Optional[list[str]] = None
        if req.tags is not None:
            cleaned = [tag.strip() for tag in req.tags if tag.strip()]
            if any(len(tag) > 32 for tag in cleaned):
                raise http_error("invalid_request", "单个标签不超过 32 字")
            tags = cleaned
        store = get_store()
        ok = store_call(store.update_rag_doc_meta, req.doc_id, user_id,
                        display_name=display_name, tags=tags)
        if not ok:
            raise http_error("ingestion_not_found", f"文档不存在：{req.doc_id}")
        audit("rag_doc_updated", request=request, actor_user_id=user_id,
              target_type="rag_doc", target_id=req.doc_id,
              detail={"has_display_name": display_name is not None,
                      "tag_count": len(tags) if tags is not None else None})
        return {"ok": True, "doc_id": req.doc_id}

    def _requeue(request: Request, doc_id: str, task: str) -> dict:
        """重建类操作共用：状态校验 + 快照前置 + 入队（复用租约/重试机制）。"""
        user_id, row = _owned_doc(request, doc_id)
        if not upload_limiter().allow(f"rag-rebuild:{user_id or client_key(request)}"):
            raise http_error("rate_limited", "操作过于频繁，稍后再试")
        store = get_store()
        if row["status"] != "ready":
            raise http_error("rag_not_ready", "文档当前状态不允许重建（处理中或已失败）",
                             detail=f"status={row['status']}")
        if task == "rechunk" and store_call(store.count_parse_snapshot, doc_id) == 0:
            raise http_error("rag_no_snapshot",
                             "该文档没有解析快照（历史文档），请重新上传后再操作")
        if not store_call(store.requeue_ingestion_for_task, row["ingestion_id"], task):
            raise http_error("rag_not_ready", "文档当前状态不允许重建",
                             detail=f"status={row['status']}")
        audit(f"rag_{task}_requested", request=request, actor_user_id=user_id,
              target_type="rag_doc", target_id=doc_id, detail={"task": task})
        return {"ok": True, "doc_id": doc_id, "ingestion_id": row["ingestion_id"],
                "task": task}

    @router.post("/api/rag/docs/rechunk")
    def rag_rechunk(req: RagRebuildRequest, request: Request) -> dict:
        """重新分块（需求 23）：解析快照 → 分块 v2 → 新版本切换（无不可检索窗口）。"""
        return _requeue(request, req.doc_id, "rechunk")

    @router.post("/api/rag/docs/reembed")
    def rag_reembed(req: RagRebuildRequest, request: Request) -> dict:
        """重新嵌入（需求 23）：同分块换向量 → 新版本切换。"""
        return _requeue(request, req.doc_id, "reembed")

    return router
