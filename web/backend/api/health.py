"""运维健康域路由（2026-11 后端按域拆分第一刀：health / live / ready）。

拆分原则（`docs/plan/2026-11-execution.md` §3 / `web/backend/api/__init__.py`）：
- 本层只做 HTTP 映射（探针、状态码、序列化），不承载业务/任务库 SQL；
- 依赖由组合根 `main.py` 通过 **getter 函数** 注入（`build_health_router`），本模块不 import
  `main`，避免反向依赖；getter 在请求时读取，兼容测试对 `main` 模块属性的 monkeypatch。
"""
from __future__ import annotations

import os
import socket
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response

#: readiness 中视为「失败」的探针状态（其余状态不 503）
_FAILED_STATUSES = {"unreachable", "invalid_url", "unavailable"}


def _probe_target(url: str, default_port: int) -> Optional[tuple[str, int]]:
    parsed = urlsplit(url)
    if not parsed.hostname:
        return None
    return parsed.hostname, parsed.port or default_port


def _tcp_reachable(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def build_health_router(
    *,
    get_store: Callable[[], Any],
    get_manager: Callable[[], Any],
    get_execution_mode: Callable[[], str],
    get_demo_mode: Callable[[], bool],
    get_worker_max_age: Callable[[], int],
    get_object_store: Callable[[], Any],
    ops_authorized: Callable[[Optional[Request]], bool],
    queue_depth: Callable[[], Optional[int]],
) -> APIRouter:
    """构造健康域路由；所有依赖以 getter 注入，便于组合根与测试替换。"""
    router = APIRouter()

    @router.get("/api/health")
    def health() -> dict:
        store = get_store()
        manager = get_manager()
        return {
            "ok": True,
            "version": "w9",
            "demo_mode": get_demo_mode(),
            "persistence": store is not None,
            "execution_mode": get_execution_mode(),
            "queue_depth": queue_depth(),
            "active_runs": manager.active_runs,
            "max_concurrent_runs": manager.max_concurrent_runs,
            "run_timeout_seconds": manager.run_timeout_seconds,
        }

    @router.get("/api/health/live")
    def health_live() -> dict:
        """liveness：进程活着即 200，不查依赖（依赖抖动不应触发编排层重启）。"""
        return {"ok": True, "check": "live"}

    @router.get("/api/health/ready")
    def health_ready(request: Request, response: Response) -> dict:
        """readiness：只对**已配置**的依赖做探针。

        - PostgreSQL：配置了任务库（P2-C）时用真实 ``SELECT 1`` 探针，否则退化为 TCP 连接；
        - Redis：TCP 探针（P3 接队列后升级为 ``PING``）；
        - ``not_configured`` 不判失败 —— 过渡期（还没接库）不会把本地与 CI 全判红；
          一旦设置但接不通即 503，由编排层摘流量。

        #337：该端点**未认证**（LB 探针要能打）⇒ ``target`` 只对持有合法 ``X-Ops-Token``
        的调用方回内网 host:port / endpoint / 异常类型；匿名调用方只看到
        ``configured`` / ``unreachable``。``status`` 字段语义不变（探针只认它）。
        """
        store = get_store()
        verbose = ops_authorized(request)

        def target(status: str, detail: str) -> str:
            if verbose:
                return detail
            return "unreachable" if status in _FAILED_STATUSES else "configured"

        checks: dict[str, dict] = {}
        for name, env_key, default_port in (
            ("postgres", "DR_DATABASE_URL", 5432),
            ("redis", "DR_REDIS_URL", 6379),
        ):
            if name == "postgres" and store is not None:
                try:
                    store.ping()
                    checks[name] = {"status": "ok", "target": target("ok", "dr_database_url")}
                except Exception as exc:  # noqa: BLE001 —— 探针失败要可归因，不裸 500
                    checks[name] = {
                        "status": "unreachable",
                        "target": target("unreachable", f"ping failed: {type(exc).__name__}"),
                    }
                continue
            raw = os.getenv(env_key, "").strip()
            if not raw:
                checks[name] = {"status": "not_configured"}
                continue
            probe = _probe_target(raw, default_port)
            if probe is None:
                checks[name] = {"status": "invalid_url"}
                continue
            host, port = probe
            status = "ok" if _tcp_reachable(host, port) else "unreachable"
            checks[name] = {"status": status, "target": target(status, f"{host}:{port}")}

        execution_mode = get_execution_mode()
        worker_max_age = get_worker_max_age()
        if execution_mode == "queue" and store is not None:
            # P1-3：队列模式下「有活跃 worker」是 readiness 的硬条件
            try:
                live = int(store.count_live_workers(within_seconds=worker_max_age))
            except Exception as exc:  # noqa: BLE001
                checks["worker"] = {
                    "status": "unreachable",
                    "target": target("unreachable", f"{type(exc).__name__}"),
                }
            else:
                checks["worker"] = {
                    "status": "ok" if live else "unavailable",
                    "live": live,
                    "max_age_seconds": worker_max_age,
                }

        object_store = get_object_store()
        if object_store is not None:
            # P1-6：配置了对象存储才探针（未配置 = 双轨回退 PG，不判失败）
            try:
                object_store.ping()
            except Exception as exc:  # noqa: BLE001
                checks["object_storage"] = {
                    "status": "unreachable",
                    "target": target("unreachable", f"{type(exc).__name__}"),
                }
            else:
                checks["object_storage"] = {
                    "status": "ok",
                    "target": target("ok", object_store.endpoint),
                }

        failed = [item for item in checks.values() if item["status"] in _FAILED_STATUSES]
        if failed:
            response.status_code = 503
        return {"ok": not failed, "status": "ready" if not failed else "not_ready",
                "checks": checks}

    return router
