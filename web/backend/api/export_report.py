"""报告同步导出路由（2026-11 后端按域拆分第十八刀：md / json / docx）。

拆分原则（`docs/plan/2026-11-execution.md` §3 / `web/backend/api/__init__.py`）：
- 装配/渲染仍走 canonical `export` / `docx_render` 模块；任务库、内存画像、产物读取由
  `main.py` 注入；本模块不 import `main`。
- 异步 PDF 作业（export-jobs）留到下一刀。
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse

from .. import docx_render as docx_render_mod
from .. import export as export_mod
from ..download_names import build_export_filename, content_disposition
from ..errors import http_error


def _markdown_response(body: str, topic: Optional[str], run_id: str) -> Response:
    """需求 17 / 77：下载文件名 = 话题名；RFC 6266 双格式（filename* UTF-8 + ASCII 回退）。"""
    display, ascii_fallback = build_export_filename(topic, run_id)
    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": content_disposition(display, ascii_fallback)},
    )

def _docx_response(body: bytes, topic: Optional[str], run_id: str) -> Response:
    """需求 38 F3：DOCX 同步导出（轻量、离线；生成在 API 进程内完成，不走作业队列）。"""
    display, ascii_fallback = build_export_filename(topic, run_id, ".docx")
    return Response(
        content=body,
        media_type=("application/vnd.openxmlformats-officedocument"
                    ".wordprocessingml.document"),
        headers={"Content-Disposition": content_disposition(display, ascii_fallback)},
    )

def export_from_store(run_id: str, fmt: str, row: dict, *,
                      artifact_text: Callable[[str, str], Optional[str]]) -> Response:
    """任务库导出回落（P2-C / 需求 38 F1）：与内存路径**同一装配与渲染口径**。"""
    if not row.get("finished_at"):
        raise http_error("report_not_ready", "研究尚未结束，暂无报告可导出")
    if fmt == "json":
        body = artifact_text(run_id, "export_json")
        if not body:
            raise http_error("report_unavailable", "本次运行没有产出报告")
        try:
            return JSONResponse(json.loads(body))
        except (TypeError, ValueError) as exc:
            raise http_error("report_unavailable", "导出载荷损坏，无法解析") from exc
    doc = export_mod.document_from_store(
        row,
        export_json=artifact_text(run_id, "export_json"),
        report_md=artifact_text(run_id, "report_md"),
    )
    if not doc.report.strip():
        raise http_error("report_unavailable", "本次运行没有产出报告")
    if fmt == "docx":
        return _docx_response(docx_render_mod.render_docx(doc), row.get("topic"), run_id)
    return _markdown_response(export_mod.render_markdown(doc), row.get("topic"), run_id)


def build_export_report_router(
    *,
    get_manager: Callable[[], Any],
    get_store: Callable[[], Any],
    store_call: Callable[..., Any],
    authorize_run: Callable[[Request, str], Any],
    enforce_output_policy: Callable[[str], None],
    artifact_text: Callable[[str, str], Optional[str]],
) -> APIRouter:
    """构造同步导出路由；依赖全部由组合根注入。"""
    router = APIRouter()

    @router.get("/api/research/{run_id}/report")
    def export_report(run_id: str, request: Request,
                      fmt: str = Query("md", alias="format",
                                       pattern="^(md|json|docx)$")) -> Response:
        """报告导出（P1-6）：`format=md` 下载 Markdown，`format=json` 取结构化载荷，
        `format=docx`（需求 38 F3）同步生成 Word 文档。

        为什么走后端而不沿用前端 Blob：前端那份只有 `result.report` 正文，
        导出的文件脱离页面后无从自证来源；后端版本带 run_id / run_status /
        降级条数等审计元数据与引用清单。P2-C：内存未命中时回落任务库产物。
        P4-A：鉴权开启时只允许导出自己的 run。P0-4：flagged / blocked 一律 403。
        """
        authorize_run(request, run_id)
        enforce_output_policy(run_id)
        manager = get_manager()
        store = get_store()
        if not manager.exists(run_id):
            if store is None:
                raise http_error("run_id_not_found", f"run_id 不存在：{run_id}")
            row = store_call(store.get_run, run_id)
            if row is None:
                raise http_error("run_id_not_found", f"run_id 不存在：{run_id}")
            return export_from_store(run_id, fmt, row, artifact_text=artifact_text)
        if not manager.has_result(run_id):
            if not manager.is_finished(run_id):
                raise http_error("report_not_ready", "研究尚未结束，暂无报告可导出")
            raise http_error("report_unavailable", "本次运行没有产出报告")
        payload = manager.export_payload(run_id)
        if payload is None or not str(payload.get("result", {}).get("report") or "").strip():
            raise http_error("report_unavailable", "本次运行没有产出报告")

        if fmt == "json":
            return JSONResponse(payload)
        if fmt == "docx":
            return _docx_response(
                docx_render_mod.render_docx(export_mod.document_from_payload(payload)),
                payload.get("topic"), run_id)
        return _markdown_response(
            manager.export_markdown(run_id) or "", payload.get("topic"), run_id)

    return router
