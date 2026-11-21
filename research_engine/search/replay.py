"""评测确定性：网络检索的 **replay provider**（录制 → 回放）。

为什么需要
----------
真实评测的检索是「活的」：同一题在不同时间会搜到不同结果，配对实验里的
before/after 差值会混入检索漂移（W8 文档已登记该限制）。本模块把一次录制好的
搜索结果按 ``query`` 精确回放，使「同 fixture + 同代码」下的检索输入确定、可复算 ——
这是评测 v2「固定证据池」的第一步（arXiv / RAG 需要各自的 replay，不在本模块范围）。

Fixture 格式（严格校验，不静默降级）
------------------------------------
``<replay_dir>/search_replay.json``::

    {
      "version": 1,
      "queries": {
        "查询文本": {
          "results": [
            {"title": "...", "url": "https://...", "snippet": "...",
             "source": "web", "metadata": {}}
          ],
          "failure_reason": null,
          "failure_detail": ""
        }
      }
    }

- 未录制的 query ⇒ ``failure_reason=empty_result`` + 明确 detail（可归因，不返回假数据）；
- ``failure_reason`` 非空时必须是 :data:`failure_reasons.TOOL_REASONS` 内的值；
- fixture 缺失/格式错误 ⇒ **构造时**抛 ``ValueError``（fail-fast，评测启动即暴露）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from research_engine.failure_reasons import TOOL_REASONS, FailureReason
from research_engine.search.base import SearchProvider, SearchResponse, SearchResult

FIXTURE_NAME = "search_replay.json"


class ReplaySearchProvider(SearchProvider):
    """按 fixture 回放网络搜索（零网络、确定性）。"""

    def __init__(self, replay_dir: str | Path):
        self.replay_dir = Path(replay_dir)
        self.fixture_path = (self.replay_dir if self.replay_dir.suffix == ".json"
                             else self.replay_dir / FIXTURE_NAME)
        self._queries = self._load()

    def _load(self) -> Dict[str, Any]:
        if not self.fixture_path.is_file():
            raise ValueError(
                f"replay fixture 不存在：{self.fixture_path}",
            )
        try:
            payload = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"replay fixture 读取/解析失败：{exc}") from exc
        queries = payload.get("queries") if isinstance(payload, dict) else None
        if not isinstance(queries, dict):
            raise ValueError("replay fixture 缺少对象字段 `queries`")
        for key, entry in queries.items():
            if not isinstance(key, str) or not isinstance(entry, dict):
                raise ValueError("replay fixture `queries` 必须为 {string: object}")
            failure = entry.get("failure_reason")
            if failure is not None and failure not in TOOL_REASONS:
                raise ValueError(
                    f"replay fixture failure_reason 非法：{failure!r}"
                    f"（仅允许 {sorted(TOOL_REASONS)} 或 null）",
                )
            for item in entry.get("results") or []:
                if not isinstance(item, dict) or not all(
                        isinstance(item.get(k), str) for k in ("title", "url", "snippet")):
                    raise ValueError(
                        f"replay fixture query={key!r} 的 results 项缺少 title/url/snippet",
                    )
        return queries

    def search(self, query: str, max_results: int = 8) -> SearchResponse:
        entry = self._queries.get(query)
        if entry is None:
            return SearchResponse(
                query=query,
                results=[],
                failure_reason=FailureReason.EMPTY_RESULT.value,
                failure_detail=f"replay fixture 未录制该查询：{query[:120]}",
            )
        failure = entry.get("failure_reason")
        if failure is not None:
            return SearchResponse(
                query=query,
                results=[],
                failure_reason=failure,
                failure_detail=str(entry.get("failure_detail") or ""),
            )
        limit = max(0, int(max_results))
        results: List[SearchResult] = []
        for item in entry.get("results") or []:
            if len(results) >= limit:
                break
            results.append(SearchResult(
                title=item["title"],
                url=item["url"],
                snippet=item["snippet"],
                source=item.get("source", "web"),
                metadata=dict(item.get("metadata") or {}),
            ))
        return SearchResponse(query=query, results=results)


def replay_dir_from_config(configured: Optional[str] = None) -> str:
    """取 replay 目录：显式参数 > ``config.search.replay_dir``；都空则报错（fail-fast）。"""
    if configured:
        return configured
    from config import config as _cfg

    value = (_cfg.search.replay_dir or "").strip()
    if not value:
        raise ValueError(
            "SEARCH_PROVIDER=replay 需要 fixture 目录：设 DR_REPLAY_DIR，"
            "或 eval CLI 传 --replay-dir",
        )
    return value
