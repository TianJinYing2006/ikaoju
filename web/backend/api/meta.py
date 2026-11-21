"""帮助中心 / 法务文档（需求 25/P7-A）：只读静态 Markdown 路由。

从 ``main.py`` 拆出的第一个域：无数据库、无会话状态，依赖只有仓库内 ``docs/legal``
与 ``docs/help`` 两个目录（内容寻址版本号对注册同意留档有约束，逻辑保持原样）。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import APIRouter

from ..errors import http_error

#: 仓库根：``web/backend/api/meta.py`` -> api -> backend -> web -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]
LEGAL_DIR = REPO_ROOT / "docs" / "legal"
HELP_DIR = REPO_ROOT / "docs" / "help"

router = APIRouter()


def legal_version(path: Path) -> str:
    """需求 25：法律文档版本号 = 内容 sha256[:12]（注册同意留档用）。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


@router.get("/api/help/faq")
def help_faq() -> dict:
    """帮助中心 FAQ（需求 25）：以仓库 `docs/help/faq.md` 为唯一来源。"""
    path = HELP_DIR / "faq.md"
    if not path.is_file():
        raise http_error("help_unavailable", "帮助文档尚未准备（请联系管理员）")
    return {"markdown": path.read_text(encoding="utf-8")}


@router.get("/api/legal/{doc}")
def legal_document(doc: str) -> dict:
    """隐私政策 / 用户协议（P7-A）：以仓库 `docs/legal/` 为唯一来源。"""
    files = {"privacy": "privacy-policy.md", "terms": "terms-of-service.md"}
    if doc not in files:
        raise http_error("invalid_request", "未知文档", detail=f"known={sorted(files)}")
    path = LEGAL_DIR / files[doc]
    if not path.is_file():
        raise http_error("invalid_request", "文档尚未准备（请联系管理员）")
    return {"doc": doc, "markdown": path.read_text(encoding="utf-8"),
            "version": legal_version(path)}
