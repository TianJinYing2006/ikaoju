"""运维域路由（2026-11 后端按域拆分第二刀：options / metrics / ops-alerts）。

拆分原则（`docs/plan/2026-11-execution.md` §3 / `web/backend/api/__init__.py`）：
- 本层只做 HTTP 映射（请求/响应/状态码/序列化），不承载业务与任务库 SQL；
- 依赖由组合根 `main.py` 通过 **getter 函数** 注入（`build_ops_router`），本模块不 import
  `main`，避免反向依赖；getter 在请求时读取，兼容测试对 `main` 模块属性的 monkeypatch。
- 只读 vs 可变的边界：`config` / `passkeys` / `profiles` / `metrics` / `alerts` 直接 import
  其 canonical 模块（测试 patch 同一模块对象即可生效）；`store` / `manager` / 环境开关等
  main 模块级单例以 getter 注入。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Optional

from fastapi import APIRouter, Request

from config import config
from research_engine.search.base import KNOWN_PROVIDERS

from .. import passkeys
from ..alerts import alert_webhook_url, collect_alerts
from ..metrics import METRICS
from ..profiles import DEFAULT_PROFILE, profile_options

#: 搜索源展示名 —— 新增 provider 时在此登记即可被前端 /api/options 列出
_PROVIDER_LABELS = {"bocha": "博查", "tavily": "Tavily"}


def _provider_has_key(name: str) -> bool:
    """该搜索源是否配了 key（决定前端是否把它列为可选）。"""
    if name == "bocha":
        return bool(config.search.bocha_api_key)
    if name == "tavily":
        return bool(config.search.tavily_api_key)
    return False


def build_ops_router(
    *,
    get_store: Callable[[], Any],
    get_manager: Callable[[], Any],
    get_execution_mode: Callable[[], str],
    get_auth_required: Callable[[], bool],
    get_share_enabled: Callable[[], bool],
    get_monthly_budget: Callable[[], float],
    get_worker_max_age: Callable[[], int],
    get_alert_thresholds: Callable[[], dict],
    require_ops: Callable[[Request], None],
    store_call: Callable[..., Any],
    queue_depth: Callable[[], Optional[int]],
) -> APIRouter:
    """构造运维域路由；所有 main 单例以 getter 注入。"""
    router = APIRouter()

    @router.get("/api/options")
    def options() -> dict:
        """前端运行选项：可用的搜索源 + 学术检索默认值。

        只把**已配 key** 的源列为可用 —— 否则用户选了没 key 的源，整场研究每跳都
        降级为零结果，跑完了才发现白跑（博查额度耗尽正是这个情形，只能靠 403 事后发现）。
        ⚠️ 已配 key ≠ 额度充足：额度耗尽只能在调用时由 provider 报出。
        """
        manager = get_manager()
        providers = [
            {"value": name, "label": _PROVIDER_LABELS.get(name, name),
             "available": _provider_has_key(name)}
            for name in KNOWN_PROVIDERS
        ]
        return {
            "search_providers": providers,
            "default_provider": config.search.provider,
            "enable_arxiv_default": config.search.enable_arxiv,
            "max_total_hops_default": config.research.max_total_hops,
            "max_subquestions_default": config.research.max_subquestions,
            # P0 profile 固化：前端只展示档位（底层参数不可提交，请求体同名字段被忽略）
            "profiles": profile_options(),
            "default_profile": DEFAULT_PROFILE,
            # P1-2 / P1-3：把后端**实际生效**的阀值下发给前端，前端才能显示剩余时间
            "run_timeout_seconds": manager.run_timeout_seconds,
            "max_concurrent_runs": manager.max_concurrent_runs,
            # P4-A / P6-A：前端据此决定是否展示登录页（未开启鉴权时保持匿名可用）
            "auth_required": get_auth_required(),
            # 需求 25：错误追踪（DSN 空 = 前端不初始化，零网络请求）
            "sentry_dsn": config.observability.sentry_dsn_frontend,
            "sentry_environment": config.observability.sentry_environment,
            "release": config.observability.sentry_release,
            # 需求 26：报告只读分享开关（前端据此隐藏分享入口）
            "share_enabled": get_share_enabled(),
            # 需求 36 H3：报告后追问开关（前端据此决定是否显示追问入口）
            "followup_enabled": bool(config.hitl.followup_enabled),
            # 需求 20 P1-2：Passkey 可用性 + RP ID
            "webauthn": {"available": passkeys.enabled(), "rp_id": passkeys.rp_id()},
        }

    def _runs_metrics_24h() -> dict:
        store = get_store()
        if store is None:
            return {}
        counts = store_call(store.status_counts_since, datetime.now(UTC) - timedelta(hours=24))
        total = sum(counts.values())
        return {
            "window_hours": 24,
            "by_status": counts,
            "total": total,
            "success_rate": round(counts.get("SUCCEEDED", 0) / total, 4) if total else None,
            "failure_rate": round(counts.get("FAILED", 0) / total, 4) if total else None,
            "timeout_rate": round(counts.get("TIMED_OUT", 0) / total, 4) if total else None,
            "lost": counts.get("LOST", 0),
        }

    @router.get("/api/metrics")
    def metrics_endpoint(request: Request) -> dict:
        """运行指标（P8-A）：进程内计数 + 任务库实时聚合；**不含任何用户内容 / PII**。

        P0-9：应用层鉴权 —— 配置 `DR_OPS_TOKEN` 时要求 `X-Ops-Token`；staging/生产
        未配置 token 一律 401（反代限制来源只作纵深）。多实例下进程内计数只代表单实例。
        """
        require_ops(request)
        store = get_store()
        snapshot = METRICS.snapshot()
        snapshot.update({
            "persistence": store is not None,
            "execution_mode": get_execution_mode(),
            "active_runs": get_manager().active_runs,
            "queue_depth": queue_depth(),
            "runs_24h": _runs_metrics_24h(),
            "stale_leases": (store_call(store.count_stale_leases)
                             if store is not None else None),
            "month_cost_cny": (round(store_call(store.month_cost_cny), 4)
                               if store is not None else None),
            "month_budget_cny": get_monthly_budget() or None,
            # P0-7：注销清理进度（pending/in_progress/completed/abandoned）
            "deletions": (store_call(store.count_deletions_by_status)
                          if store is not None else None),
            # P1-3：活跃 worker 数（心跳新鲜度窗口见 DR_WORKER_HEARTBEAT_MAX_AGE_SECONDS）
            "workers_live": (store_call(store.count_live_workers,
                                        within_seconds=get_worker_max_age())
                             if store is not None else None),
        })
        return snapshot

    @router.get("/api/ops/alerts")
    def ops_alerts(request: Request) -> dict:
        """告警判定（P8-A / P2-6）：按阈值评估当前指标；**外送**由 Worker 统一走 webhook。

        P0-9：与 `/api/metrics` 同一应用层鉴权（DR_OPS_TOKEN / fail-closed）。
        """
        require_ops(request)
        store = get_store()
        queue = queue_depth()
        stale = store_call(store.count_stale_leases) if store is not None else 0
        monthly = store_call(store.month_cost_cny) if store is not None else 0.0
        deletions = store_call(store.count_deletions_by_status) if store is not None else {}
        workers_live = (
            store_call(store.count_live_workers, within_seconds=get_worker_max_age())
            if store is not None else None
        )
        overdue_appeals = (
            store_call(store.count_appeals_overdue, datetime.now(UTC))
            if store is not None else 0
        )
        alerts = collect_alerts(
            thresholds=get_alert_thresholds(),
            http=METRICS.snapshot()["http"],
            queue_depth=queue,
            stale_leases=stale,
            deletions=deletions,
            month_cost=monthly,
            month_budget=get_monthly_budget(),
            workers_live=workers_live,
            execution_mode=get_execution_mode(),
            overdue_appeals=overdue_appeals,
        )
        return {"ok": True, "alert_count": len(alerts), "alerts": alerts,
                "webhook_configured": bool(alert_webhook_url())}

    return router
