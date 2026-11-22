"""知识库上传路由（2026-11 后端按域拆分第十五刀：流式落盘 + 三重校验 + 异步登记）。

拆分原则（`docs/plan/2026-11-execution.md` §3 / `web/backend/api/__init__.py`）：
- 本层保留安全关键的上传编排（流式落盘/类型与 magic 校验/孤儿清理），但任务库、
  限流、鉴权、隔离区路径由 `main.py` 或 canonical 模块提供；
- 本模块不 import `main`。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from typing import Any, Callable

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from ..errors import http_error, safe_detail
from ..upload_guard import (
    ALLOWED_EXT,
    UploadRejected,
    detect_and_validate,
    extension_of,
    sanitize_filename,
)


def build_rag_ingest_router(
    *,
    get_store: Callable[[], Any],
    store_call: Callable[..., Any],
    require_user: Callable[[Request], Any],
    check_csrf: Callable[[Request], None],
    client_key: Callable[[Request], str],
    upload_limiter: Callable[[], Any],
    max_upload_mb: Callable[[], float],
    stream_to_temp_fn: Callable[..., Any],
) -> APIRouter:
    """构造知识库上传路由；依赖全部由组合根注入。"""
    router = APIRouter()

    @router.post("/api/rag/ingest")
    async def rag_ingest(request: Request, file: UploadFile = File(...)) -> Response:
        """上传并摄取文档（P6-A / P0-8a / P0-8b）：流式落盘 + 三重校验 + 异步管线。

        - 有任务库（staging/生产）：写入隔离区 + 登记 `rag_ingestions` ⇒ **202**，
          由 Worker 异步解析 / embedding / 写 Qdrant；`GET /api/rag/ingestions/{id}` 查状态；
          相同内容（内容寻址 doc_id）重复上传命中既有记录，不重复处理。
        - 无任务库（本地零依赖）：退化为同步摄取 ⇒ **200**（行为与 P6-A 一致）。
        - 三重校验与解析限额同 P0-8a；隔离区目录由 `DR_RAG_QUARANTINE_DIR` 配置。
        """
        user_id = require_user(request)
        check_csrf(request)
        if not upload_limiter().allow(f"rag:{user_id or client_key(request)}"):
            raise http_error("rate_limited", "上传过于频繁，稍后再试")

        filename = sanitize_filename(file.filename)
        extension = extension_of(filename)
        if extension not in ALLOWED_EXT:
            raise http_error(
                "unsupported_file_type", "不支持的文件类型",
                detail=f"allowed={sorted(ALLOWED_EXT)}",
            )

        store = get_store()
        max_bytes = int(max_upload_mb() * 1024 * 1024)
        if store is not None:
            from ..ingestion import quarantine_dir

            stored_name = f"{uuid.uuid4().hex}{extension}"
            path = os.path.join(quarantine_dir(), stored_name)
        else:
            stored_name = ""
            tmp_dir = tempfile.mkdtemp(prefix="dr-rag-")
            path = os.path.join(tmp_dir, f"{uuid.uuid4().hex}{extension}")

        #: #325：只有真正登记成功（202/200）后隔离区文件才交给 worker，不得删；
        #: 写盘后的任何拒绝路径（400/413/500）都必须清掉，否则留下无 DB 记录的孤儿文件。
        registered = False
        try:
            try:
                size, digest, head = await stream_to_temp_fn(file, path, max_bytes)
            except UploadRejected as exc:
                raise http_error(exc.code, exc.message, detail=exc.detail) from exc
            except OSError as exc:
                # 2026-11：隔离区卷不可写 / 目录缺失（如 named volume 属主错误）⇒ 结构化 503，
                # 不裸露 500。客户端只回固定文案；异常类型与原文经 safe_detail 进服务端日志（#324）。
                raise http_error(
                    "rag_ingest_failed",
                    safe_detail("上传暂存失败（服务端存储不可写）", exc),
                ) from exc
            if size == 0:
                raise http_error("invalid_request", "文件为空")
            try:
                detect_and_validate(path, filename, head)
            except UploadRejected as exc:
                raise http_error(exc.code, exc.message, detail=exc.detail) from exc

            doc_id = f"{user_id or 'local'}:{digest[:16]}"
            if store is not None:
                # P0-8b / P0-11：登记后由 Worker 异步处理。去重走**数据库唯一索引**
                # （`ON CONFLICT ... DO NOTHING`，见 store.create_ingestion + 0017）：
                # 并发上传不可能重复插入，冲突方回查既有记录并删除自己的隔离区文件。
                ingestion_id = uuid.uuid4().hex[:12]
                try:
                    created = store.create_ingestion(
                        ingestion_id, doc_id, user_id=user_id, source=filename,
                        sha256=digest, size_bytes=size, stored_name=stored_name)
                except Exception as exc:  # noqa: BLE001
                    os.remove(path)
                    raise http_error(
                        "persistence_unavailable",
                        safe_detail("摄取登记失败", exc),
                    ) from exc
                if created is None:
                    existing = store_call(store.find_ingestion_by_doc, user_id, doc_id)
                    os.remove(path)
                    registered = True  # 去重命中：本文件已删，别让 finally 重复处理
                    if existing is None:
                        raise http_error("persistence_unavailable", "摄取登记冲突，请重试")
                    return JSONResponse(status_code=202, content={
                        "ingestion_id": existing["ingestion_id"], "doc_id": doc_id,
                        "source": existing["source"], "status": existing["status"],
                    })
                registered = True
                return JSONResponse(status_code=202, content={
                    "ingestion_id": ingestion_id, "doc_id": doc_id,
                    "source": filename, "status": "pending",
                })

            # 本地零依赖：同步摄取（P6-A 行为）
            import asyncio

            from research_engine.rag.ingest import DocumentIngester, IngestLimitExceeded

            def _run_ingest() -> int:
                return DocumentIngester().ingest_file(path, doc_id=doc_id, user_id=user_id)

            try:
                chunks = await asyncio.get_running_loop().run_in_executor(None, _run_ingest)
            except IngestLimitExceeded as exc:
                raise http_error("document_limit_exceeded", str(exc)[:200]) from exc
            except Exception as exc:  # noqa: BLE001
                raise http_error(
                    "rag_ingest_failed",
                    safe_detail("文档摄取失败", exc),
                ) from exc
            if not chunks:
                raise http_error("rag_ingest_failed", "文档未解析出任何内容")
            return JSONResponse(status_code=200, content={
                "doc_id": doc_id, "source": filename, "chunks": chunks,
            })
        finally:
            if store is None:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            elif not registered and os.path.exists(path):
                os.remove(path)  # #325：拒绝路径不留隔离区（无 DB 记录 = 永久孤儿）

    return router
