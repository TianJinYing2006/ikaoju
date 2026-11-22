"""异步 PDF 导出作业路由（2026-11 后端按域拆分第十九刀：export 域收官）。

拆分原则（`docs/plan/2026-11-execution.md` §3 / `web/backend/api/__init__.py`）：
- 作业台账/结果通道仍由任务库与专用 Redis 提供；本模块只做 HTTP 映射、幂等复用、
  输入预检与下载装配。结果通道客户端由组合根注入（保留同名 monkeypatch 语义）。
- 本模块不 import `main`。
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any, Callable, Optional

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from ..download_names import build_export_filename, content_disposition
from ..errors import http_error, safe_detail

_WAKE_KEY = "dr:export:wake"
_RESULT_KEY_PREFIX = "dr:export:result:"
_export_wake_redis = None


class ExportJobRequest(BaseModel):
    """v1 仅支持 kind=pdf（DOCX 为轻量同步生成，不走作业）。"""

    kind: str = Field("pdf", pattern="^pdf$")


def build_export_jobs_router(
    *,
    get_export_store: Callable[[], Any],
    get_store: Callable[[], Any],
    get_manager: Callable[[], Any],
    store_call: Callable[..., Any],
    authorize_run: Callable[[Request, str], Any],
    check_csrf: Callable[[Request], None],
    enforce_output_policy: Callable[[str], None],
    audit: Callable[..., None],
    artifact_text: Callable[[str, str], Optional[str]],
    get_result_store: Callable[[], Any],
    max_input_bytes: Callable[[], int],
    result_ttl: Callable[[], int],
    limiter_redis_url: Callable[[], str],
) -> APIRouter:
    """构造异步导出作业路由；依赖全部由组合根注入。"""
    router = APIRouter()

    def _push_wake() -> None:
        """唤醒信号（主 Redis；best-effort —— 丢失由 export 服务 2s 轮询兜底）。"""
        global _export_wake_redis
        url = limiter_redis_url()
        if not url:
            return
        try:
            if _export_wake_redis is None:
                import redis as redis_lib

                _export_wake_redis = redis_lib.Redis.from_url(url)
            _export_wake_redis.lpush(_WAKE_KEY, "1")
        except Exception:  # noqa: BLE001 —— 唤醒是旁路
            pass

    def _job_or_404(run_id: str, export_id: str) -> dict:
        es = get_export_store()
        if es is None:
            raise http_error("persistence_unavailable", "导出作业需要任务库（DR_DATABASE_URL）")
        job = store_call(es.get_export_job, export_id)
        if job is None or job.get("run_id") != run_id:
            raise http_error("export_job_not_found", f"导出作业不存在：{export_id}")
        return job

    @router.post("/api/research/{run_id}/export-jobs", status_code=202)
    def create_export_job(run_id: str, req: ExportJobRequest, request: Request) -> dict:
        """需求 38 F2：创建 PDF 导出作业（异步；202 + export_id）。

        - 前置：所有权 + CSRF + 输出 gate（flagged/under-review 阻断，与导出同口径）
          + run 终局且有报告 + **输入字节预检**（早拒）；
        - 幂等：同一 run 的进行中作业或未过期结果直接复用（request_hash 唯一约束兜底并发）；
        - 结果 bytes 由专用 export 服务写专用非持久化 Redis；PG 保存权威状态与结果元数据。
        """
        owner = authorize_run(request, run_id) or ""
        check_csrf(request)
        enforce_output_policy(run_id)
        es = get_export_store()
        if es is None:
            raise http_error("persistence_unavailable", "导出作业需要任务库（DR_DATABASE_URL）")
        row = store_call(es.get_run, run_id)
        if row is None:
            raise http_error("run_id_not_found", f"run_id 不存在：{run_id}")
        if not row.get("finished_at"):
            raise http_error("report_not_ready", "研究尚未结束，暂无报告可导出")
        if get_store() is None:  # 内存 / DR_DEMO：报告在 manager，无持久化产物可预检
            if not get_manager().has_result(run_id):
                raise http_error("report_unavailable", "本次运行没有产出报告")
        else:
            report = artifact_text(run_id, "report_md") or ""
            if not report.strip():
                raise http_error("report_unavailable", "本次运行没有产出报告")
            max_bytes = max_input_bytes()
            if len(report.encode()) > max_bytes:
                raise http_error(
                    "export_too_large",
                    f"报告超过 PDF 导出上限（{max_bytes} 字节），请改用 md/json",
                    detail=f"report_bytes={len(report.encode())}")
        request_hash = hashlib.sha256(f"{run_id}|pdf|{owner}".encode()).hexdigest()
        existing = store_call(es.find_reusable_export_job, request_hash)
        if existing is not None:
            return {"export_id": existing["job_id"], "status": existing["status"], "reused": True}
        job_id = uuid.uuid4().hex[:12]
        try:
            job = store_call(es.create_export_job, job_id, run_id=run_id,
                             user_id=owner, request_hash=request_hash)
        except Exception as exc:  # noqa: BLE001 —— 并发同指纹冲突（唯一索引）⇒ 复用既有作业
            if getattr(exc, "sqlstate", None) == "23505":
                existing = store_call(es.find_reusable_export_job, request_hash)
                if existing is not None:
                    return {"export_id": existing["job_id"], "status": existing["status"],
                            "reused": True}
            raise http_error(
                "persistence_unavailable",
                safe_detail("导出作业创建失败", exc)) from exc
        _push_wake()
        audit("export_job_created", request=request, actor_user_id=owner or None,
              target_type="run", target_id=run_id,
              detail={"export_id": job_id, "kind": "pdf"})
        return {"export_id": job["job_id"], "status": job["status"], "reused": False}

    @router.get("/api/research/{run_id}/export-jobs/{export_id}")
    def get_export_job(run_id: str, export_id: str, request: Request) -> dict:
        """查询 PDF 导出作业状态（所有权 + 输出 gate 同口径）。"""
        authorize_run(request, run_id)
        enforce_output_policy(run_id)
        job = _job_or_404(run_id, export_id)
        expires = job.get("result_expires_at")
        return {
            "export_id": job["job_id"],
            "status": job["status"],
            "error_code": job.get("error_code"),
            "result_bytes": job.get("result_bytes"),
            "result_expires_at": expires.isoformat(timespec="seconds") if expires else None,
        }

    @router.get("/api/research/{run_id}/export-jobs/{export_id}/download")
    def download_export_job(run_id: str, export_id: str, request: Request) -> Response:
        """下载 PDF 导出结果（**每次**复核所有权 + 输出 gate + 结果有效期）。

        结果过期/丢失 ⇒ `export_result_expired`（结构化错误；用户可重新申请再生成）；
        不退回 API 同步渲染。`Cache-Control: private, no-store`。
        """
        authorize_run(request, run_id)
        enforce_output_policy(run_id)
        job = _job_or_404(run_id, export_id)
        if job.get("status") != "SUCCEEDED":
            raise http_error("export_not_ready", "导出尚未完成",
                             detail=f"status={job.get('status')}")
        expires = job.get("result_expires_at")
        if expires is None or expires <= datetime.now(UTC):
            raise http_error("export_result_expired", "导出结果已过期，请重新申请")
        client = get_result_store()
        if client is not None:
            try:
                pdf = client.get(_RESULT_KEY_PREFIX + export_id)
            except Exception as exc:  # noqa: BLE001
                raise http_error(
                    "export_unavailable", f"结果通道读取失败：{type(exc).__name__}") from exc
        else:
            # DR_DEMO / 内存替身：结果随作业对象保存；真实模式未配通道 ⇒ 结构化不可用
            get_result = getattr(get_export_store(), "get_result", None)
            if get_result is None:
                raise http_error("export_unavailable", "结果通道未配置（DR_EXPORT_REDIS_URL）")
            pdf = get_result(export_id)
        if not pdf:
            raise http_error("export_result_expired", "导出结果已过期或丢失，请重新申请")
        topic = None
        es = get_export_store()
        if es is not None:
            row = store_call(es.get_run, run_id) or {}
            topic = row.get("topic")
        if not topic:
            try:  # 内存 / DR_DEMO：话题名从 manager 载荷取；取不到回退 run_id 文件名
                topic = (get_manager().export_payload(run_id) or {}).get("topic")
            except Exception:  # noqa: BLE001 —— 文件名回退不影响下载本体
                topic = None
        display, ascii_fallback = build_export_filename(topic, run_id, ".pdf")
        return Response(
            content=pdf,
            media_type="application/pdf",
            headers={
                "Content-Disposition": content_disposition(display, ascii_fallback),
                "Cache-Control": "private, no-store",
            },
        )

    return router
