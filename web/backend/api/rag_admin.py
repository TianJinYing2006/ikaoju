"""知识库管理路由（2026-11 后端按域拆分第十四刀：摄取状态 / 删除文档）。

拆分原则（`docs/plan/2026-11-execution.md` §3 / `web/backend/api/__init__.py`）：
- 本层只做 HTTP 映射与序列化；任务库、鉴权开关、失败文案由 `main.py` 注入；
- 本模块不 import `main`；Qdrant 删除的「删除并验证归零」逻辑随本刀迁入。
"""
from __future__ import annotations

import os
from typing import Any, Callable, Optional

from fastapi import APIRouter, Query, Request

from ..errors import http_error, safe_detail


def build_rag_admin_router(
    *,
    get_store: Callable[[], Any],
    store_call: Callable[..., Any],
    require_user: Callable[[Request], Optional[str]],
    check_csrf: Callable[[Request], None],
    format_dt: Callable[[Any], Optional[str]],
    failed_detail: Callable[[], str],
    auth_required: Callable[[], bool],
) -> APIRouter:
    """构造知识库管理路由；依赖全部由组合根注入。"""
    router = APIRouter()

    @router.get("/api/rag/ingestions/{ingestion_id}")
    def rag_ingestion_status(ingestion_id: str, request: Request) -> dict:
        """摄取状态（P0-8b）：前端轮询 pending → processing → ready | rejected。"""
        store = get_store()
        if store is None:
            raise http_error("persistence_unavailable", "摄取状态需要任务库（DR_DATABASE_URL）")
        user_id = require_user(request)
        row = store_call(store.get_ingestion, ingestion_id)
        if row is None or (auth_required() and row.get("user_id") != user_id):
            raise http_error("ingestion_not_found", f"摄取记录不存在：{ingestion_id}")
        return {
            "ingestion_id": row["ingestion_id"],
            "doc_id": row["doc_id"],
            "source": row["source"],
            "status": row["status"],
            "task": row.get("task") or "ingest",
            "chunks": row["chunks"],
            "attempts": row["attempts"],
            "scan_status": row["scan_status"],
            "active_generation": row.get("active_generation"),
            "error": failed_detail() if row.get("last_error") else None,
            "created_at": format_dt(row.get("created_at")),
            "processed_at": format_dt(row.get("processed_at")),
        }

    @router.delete("/api/rag/docs")
    def rag_delete_doc(request: Request, doc_id: str = Query(..., max_length=128)) -> dict:
        """删除知识库文档（P0-8b）：Qdrant 按 doc_id 删除并**验证归零**，再清隔离区文件。

        同步删除 + 失败 503（用户在场可重试）；账号注销走 durable outbox（P0-7）。
        """
        store = get_store()
        if store is None:
            raise http_error("persistence_unavailable", "文档删除需要任务库（DR_DATABASE_URL）")
        user_id = require_user(request)
        check_csrf(request)
        if doc_id.split(":", 1)[0] != (user_id or "local"):
            raise http_error("invalid_request", "无权删除该文档", detail="ownership mismatch")

        from research_engine.rag.store import VectorStore

        from ..ingestion import quarantine_path

        vector_store = VectorStore()
        reason = vector_store.unavailable_reason
        if reason:
            raise http_error("rag_unavailable", "知识库当前不可用，稍后再试",
                             detail=vector_store.last_error or reason)
        try:
            vector_store.delete_by_doc(doc_id, wait=True)
            remaining = vector_store.count_by_doc(doc_id)
            if remaining:
                raise RuntimeError(f"still has {remaining} points")
        except Exception as exc:  # noqa: BLE001 —— 删除失败如实报错，用户可重试
            raise http_error(
                "rag_unavailable",
                safe_detail("文档删除失败", exc),
            ) from exc

        names = store_call(store.delete_ingestion_by_doc, user_id, doc_id)
        for name in names:
            if not name:
                continue
            try:
                os.remove(quarantine_path(name))
            except FileNotFoundError:
                pass
        # 需求 23：三层 PG 数据一并删除；递增修订号使检索缓存失效（删除后不可命中）
        store_call(store.delete_rag_layers, doc_id)
        store_call(store.bump_rag_revision)
        return {"ok": True, "doc_id": doc_id}

    return router
