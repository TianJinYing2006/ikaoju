"""知识库只读路由（2026-11 后端按域拆分第十二刀：文档清单 / 分块预览 / 用量）。

拆分原则（`docs/plan/2026-11-execution.md` §3 / `web/backend/api/__init__.py`）：
- 本层只做 HTTP 映射与序列化；任务库/Qdrant 访问、失败详情口径由 `main.py` 注入；
- 本模块不 import `main`；本地零依赖时回落 Qdrant 聚合的逻辑随本刀迁入。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Query, Request

from ..errors import http_error


def build_rag_read_router(
    *,
    get_store: Callable[[], Any],
    store_call: Callable[..., Any],
    require_user: Callable[[Request], Optional[str]],
    format_dt: Callable[[Any], Optional[str]],
    failed_detail: Callable[[], str],
    total_quota_mb: Callable[[], float],
) -> APIRouter:
    """构造知识库只读路由；依赖全部由组合根注入。"""
    router = APIRouter()

    def _docs_from_qdrant(user_id: Optional[str]) -> dict:
        """本地零依赖回落：无任务库时从 Qdrant 聚合（无台账字段）。"""
        from research_engine.rag.scope import RagScope
        from research_engine.rag.store import VectorStore

        vector_store = VectorStore()
        reason = vector_store.unavailable_reason
        if reason:
            raise http_error(
                "rag_unavailable",
                "知识库当前不可用（Qdrant 未配置或连不上）",
                detail=vector_store.last_error or reason,
            )
        counts: dict[str, dict] = {}
        for payload in vector_store.scroll_all(scope=RagScope(user_id=user_id)):
            doc_id = str(payload.get("doc_id") or "")
            source = payload.get("source") or doc_id or "(未命名)"
            key = doc_id or source
            entry = counts.setdefault(key, {"doc_id": doc_id, "source": source, "chunks": 0})
            entry["chunks"] += 1
        return {"docs": sorted(counts.values(), key=lambda item: item["source"])}

    @router.get("/api/rag/docs")
    def rag_docs(request: Request) -> dict:
        """知识库文档清单（需求 23）：**PG 台账为准**；本地零依赖回落 Qdrant 聚合。

        返回状态 / 大小 / 块数 / 失败原因 / 活动版本 / 展示名 / 标签 —— 上传后不再是黑盒。
        """
        user_id = require_user(request)
        store = get_store()
        if store is None:
            return _docs_from_qdrant(user_id)
        rows = store_call(store.list_rag_docs, user_id)
        return {"docs": [
            {
                "doc_id": row["doc_id"],
                "source": row["source"],
                "display_name": row.get("display_name"),
                "tags": list(row.get("tags") or []),
                "status": row["status"],
                "chunks": row.get("chunks") or 0,
                "size_bytes": row.get("size_bytes"),
                "created_at": format_dt(row.get("created_at")),
                "error": failed_detail() if row.get("last_error") else None,
                "active_generation": row.get("active_generation"),
            }
            for row in rows
        ]}

    @router.get("/api/rag/docs/{doc_id}/chunks")
    def rag_doc_chunks(doc_id: str, request: Request,
                       generation: Optional[int] = Query(None, ge=1),
                       offset: int = Query(0, ge=0),
                       limit: int = Query(20, ge=1, le=100)) -> dict:
        """分块预览（需求 23）：默认活动版本；locator 供引用回溯定位。"""
        store = get_store()
        if store is None:
            raise http_error("persistence_unavailable", "分块预览需要任务库（DR_DATABASE_URL）")
        user_id = require_user(request)
        row = store_call(store.get_rag_doc_for_user, doc_id, user_id)
        if row is None:
            raise http_error("ingestion_not_found", f"文档不存在：{doc_id}")
        active = row.get("active_generation")
        target = generation or active
        if target is None:
            return {"doc_id": doc_id, "generation": None, "active_generation": active,
                    "total": 0, "chunks": []}
        chunks, total = store_call(store.list_rag_chunks, doc_id, int(target),
                                   offset=offset, limit=limit)
        return {
            "doc_id": doc_id,
            "generation": int(target),
            "active_generation": active,
            "total": total,
            "chunks": [
                {"chunk_index": item["chunk_index"], "chunk_id": item["chunk_id"],
                 "text": item["text"], "locator": item.get("locator") or {}}
                for item in chunks
            ],
        }

    @router.get("/api/rag/usage")
    def rag_usage(request: Request) -> dict:
        """知识库用量（需求 23）：已用字节 + 可选总配额（`DR_RAG_TOTAL_MB`，0=不限）。"""
        user_id = require_user(request)
        store = get_store()
        used = store_call(store.rag_usage_bytes, user_id) if store is not None else 0
        total_mb = total_quota_mb()
        quota = int(total_mb * 1024 * 1024) if total_mb and total_mb > 0 else None
        return {"used_bytes": used, "quota_bytes": quota}

    return router
