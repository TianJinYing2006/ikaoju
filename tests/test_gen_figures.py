# -*- coding: utf-8 -*-
"""figures 结构事实生成器（`tools/gen_figures.py`）的单测。

设计意图与 `test_doc_consistency.py` 相同：生成器/守卫的价值全在「它会不会漏」。
因此本文件的重点是**负向用例**：JSON 过期、figures.md 行缺失/计数错误、git 不可用、
缺生成物都必须报错，而不是静默通过。

用 `tmp_path` 迷你仓库 + 注入 `git_runner`：零真实 git 依赖、零网络、毫秒级。
git 事实（`tracked_python_files/lines`）与文件系统事实分离，正是为了能这样测。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPO_ROOT / "tools" / "gen_figures.py"

REQUIRED_DIRS = (
    "docs/decisions",
    "docs/requirements",
    "migrations",
    "migrations/checks",
    "tests",
    "web/frontend/e2e",
    ".github/workflows",
)


def _load_tool():
    """tools/ 不是包，按文件路径加载（与 test_doc_consistency.py 同法）。"""
    spec = importlib.util.spec_from_file_location("gen_figures", TOOL_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - 防御性
        raise RuntimeError(f"无法加载 {TOOL_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOL = _load_tool()


# ---------------------------------------------------------------------------
# 迷你仓库 / 假 git
# ---------------------------------------------------------------------------
def _figures_md(
    *,
    adr: int = 2,
    requirements: int = 3,
    migrations: int = 2,
    migration_checks: int = 2,
    test_files: int = 2,
    ci_guards: int = 2,
) -> str:
    """与 `_build_repo` / `_facts` 对齐的合成 figures.md（ADR 用加粗验证宽松解析）。"""
    return "\n".join(
        [
            "# 对外数字唯一表（figures）",
            "",
            "| 指标 | 当前值 | 口径 / 定义 | 采集命令或来源 | 日期 |",
            "| --- | --- | --- | --- | --- |",
            f"| 需求文档 | {requirements} 篇 + 1 模板 | `docs/requirements/*.md` | 目录计数 | 2026-11-03 |",
            f"| ADR | **{adr}** 篇 | `docs/decisions/0*.md` | 目录计数 | 2026-11-03 |",
            f"| 数据库迁移 | {migrations} 个 + {migration_checks} 个结构断言 | `migrations/*.sql` | 目录计数 | 2026-11-03 |",
            f"| 测试文件 | {test_files} 个 | `tests/*.py` | 目录计数 | 2026-11-03 |",
            f"| CI 守卫 | {ci_guards} 个 | `tools/check_*.py` | CI workflow | 2026-11-03 |",
            "",
        ]
    )


def _facts(**overrides: object) -> dict[str, object]:
    facts: dict[str, object] = {
        "adr_count": 2,
        "ci_guard_script_files": ["check_alpha.py", "check_beta.py"],
        "ci_guard_scripts": 2,
        "e2e_spec_files": 1,
        "migration_checks": 2,
        "migrations": 2,
        "requirement_docs": 3,
        "test_files": 2,
        "tracked_python_files": 2,
        "tracked_python_lines": 4,
    }
    facts.update(overrides)
    return facts


def _fake_git(tracked: tuple[str, ...] = ("pkg/mod.py", "main.py")):
    """假 git：只回答 tracked Python 清单；单测只注入两条，未跟踪文件不参与。"""

    def runner(root: Path, *args: str) -> str:
        assert args == ("ls-files", "*.py"), args
        return "\n".join(sorted(tracked)) + "\n"

    return runner


def _build_repo(root: Path, *, test_files: int = 2) -> None:
    for rel in REQUIRED_DIRS:
        (root / rel).mkdir(parents=True, exist_ok=True)

    # ADR：只应匹配 0DDD-*.md，TEMPLATE/README 不算
    (root / "docs/decisions/0001-alpha.md").write_text("# 0001\n", encoding="utf-8")
    (root / "docs/decisions/0012-beta.md").write_text("# 0012\n", encoding="utf-8")
    (root / "docs/decisions/TEMPLATE.md").write_text("# template\n", encoding="utf-8")

    # 需求文档：模板.md 排除
    for index in range(1, 4):
        (root / f"docs/requirements/{index}-req.md").write_text("", encoding="utf-8")
    (root / "docs/requirements/模板.md").write_text("", encoding="utf-8")

    # 迁移 + 结构断言
    (root / "migrations/0001_a.sql").write_text("", encoding="utf-8")
    (root / "migrations/0002_b.sql").write_text("", encoding="utf-8")
    (root / "migrations/checks/0001_a.sql").write_text("", encoding="utf-8")
    (root / "migrations/checks/0002_b.sql").write_text("", encoding="utf-8")

    # 测试文件：只数 test_*.py（conftest.py 不算）
    for index in range(test_files):
        (root / f"tests/test_{index}.py").write_text("", encoding="utf-8")
    (root / "tests/conftest.py").write_text("", encoding="utf-8")

    # E2E spec
    (root / "web/frontend/e2e/smoke.spec.ts").write_text("", encoding="utf-8")

    # CI 守卫：注释行不算；重复调用去重；check_ignored 只在注释里
    (root / ".github/workflows/ci.yml").write_text(
        "jobs:\n"
        "  lint:\n"
        "    steps:\n"
        "      - run: python tools/check_alpha.py\n"
        "      # - run: python tools/check_ignored.py\n"
        "      - run: python3 tools/check_beta.py --strict\n"
        "      - run: python tools/check_alpha.py\n",
        encoding="utf-8",
    )

    # git 跟踪的 Python（假 git 只认这俩）：main.py 1 行 + pkg/mod.py 3 行 = 4 行
    (root / "main.py").write_text("print(1)\n", encoding="utf-8")
    (root / "pkg").mkdir()
    (root / "pkg/mod.py").write_text("import os\n\nVALUE = 1\n", encoding="utf-8")

    (root / "docs/figures.md").write_text(_figures_md(test_files=test_files), encoding="utf-8")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _build_repo(root)
    return root


# ---------------------------------------------------------------------------
# collect_facts：计数正确、可重复
# ---------------------------------------------------------------------------
def test_collect_facts_counts(repo: Path) -> None:
    assert TOOL.collect_facts(repo, git_runner=_fake_git()) == _facts()


def test_collect_facts_is_deterministic(repo: Path) -> None:
    first = TOOL.collect_facts(repo, git_runner=_fake_git())
    second = TOOL.collect_facts(repo, git_runner=_fake_git())
    assert first == second


def test_collect_facts_reports_tracked_python_lines(repo: Path) -> None:
    facts = TOOL.collect_facts(repo, git_runner=_fake_git())
    assert facts["tracked_python_files"] == 2
    assert facts["tracked_python_lines"] == 4  # main.py(1) + pkg/mod.py(3)


def test_missing_required_dir_fails_loud(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    with pytest.raises(TOOL.FiguresError, match="docs/decisions"):
        TOOL.collect_facts(root, git_runner=_fake_git())


def test_missing_tracked_python_file_fails_loud(repo: Path) -> None:
    runner = _fake_git(("missing.py",))
    with pytest.raises(TOOL.FiguresError, match="缺失"):
        TOOL.collect_facts(repo, git_runner=runner)


# ---------------------------------------------------------------------------
# 生成物：确定性 JSON / UTF-8 / 末尾换行 / 无时间戳
# ---------------------------------------------------------------------------
def test_render_generated_is_canonical(repo: Path) -> None:
    facts = TOOL.collect_facts(repo, git_runner=_fake_git())
    text = TOOL.render_generated(facts)
    assert text == TOOL.render_generated(facts)  # 两次渲染逐字节一致
    assert text.endswith("\n") and not text.endswith("\n\n")
    assert "\r" not in text
    data = json.loads(text)
    assert data == facts
    assert list(data) == sorted(data)  # sort_keys
    assert "timestamp" not in text  # 无时间戳


def test_write_generated_bytes_roundtrip(repo: Path) -> None:
    facts = TOOL.collect_facts(repo, git_runner=_fake_git())
    path = TOOL.write_generated(repo, facts)
    raw = path.read_bytes()
    assert path == repo / TOOL.GENERATED_REL
    assert raw == TOOL.render_generated(facts).encode("utf-8")
    assert raw.endswith(b"\n") and b"\r" not in raw
    assert json.loads(raw.decode("utf-8")) == facts


# ---------------------------------------------------------------------------
# 第 2 层：JSON -> figures.md
# ---------------------------------------------------------------------------
def test_check_against_figures_aligned_passes() -> None:
    assert TOOL.check_against_figures(_facts(), _figures_md()) == []


def test_missing_figures_row_fails_with_label() -> None:
    figures = "\n".join(
        line for line in _figures_md().splitlines() if not line.startswith("| ADR ")
    )
    errors = TOOL.check_against_figures(_facts(), figures)
    assert any("ADR" in error and "缺少" in error for error in errors)


def test_wrong_figures_count_fails_with_label() -> None:
    errors = TOOL.check_against_figures(_facts(), _figures_md(requirements=4))
    assert any("需求文档" in error and "4" in error and "3" in error for error in errors)


def test_wrong_migration_check_count_fails() -> None:
    errors = TOOL.check_against_figures(_facts(), _figures_md(migration_checks=1))
    assert any("结构断言" in error for error in errors)


def test_missing_migration_check_count_fails_loud() -> None:
    figures = _figures_md().replace("2 个 + 2 个结构断言", "2 个")
    errors = TOOL.check_against_figures(_facts(), figures)
    assert any("数据库迁移" in error and "结构断言" in error for error in errors)


def test_wrong_ci_guard_count_fails_with_label() -> None:
    errors = TOOL.check_against_figures(_facts(), _figures_md(ci_guards=1))
    assert any("CI 守卫" in error for error in errors)


def test_duplicate_figures_row_fails_loud() -> None:
    figures = _figures_md() + "| ADR | **2** 篇 | 重复行 | x | 2026-11-03 |\n"
    errors = TOOL.check_against_figures(_facts(), figures)
    assert any("ADR" in error and "出现 2 次" in error for error in errors)


# ---------------------------------------------------------------------------
# 两层 --check
# ---------------------------------------------------------------------------
def test_check_repo_green_after_write(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(TOOL, "_run_git", _fake_git())
    assert TOOL.main(["--root", str(repo), "--write"]) == 0
    assert (repo / TOOL.GENERATED_REL).is_file()
    assert TOOL.check_repo(repo) == []
    assert TOOL.main(["--root", str(repo), "--check"]) == 0
    assert TOOL.main(["--root", str(repo)]) == 0  # 默认行为 = --check


def test_check_repo_without_generated_json_fails(repo: Path) -> None:
    errors = TOOL.check_repo(repo, git_runner=_fake_git())
    assert any("figures.generated.json" in error and "--write" in error for error in errors)


def test_live_drift_reported_as_stale_json_only(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(TOOL, "_run_git", _fake_git())
    TOOL.write_generated(repo, TOOL.collect_facts(repo, git_runner=_fake_git()))
    assert TOOL.check_repo(repo) == []

    # 新增测试文件：live 结构变了，JSON 未重生成 => 第 1 层失败；figures.md 仍对齐旧 JSON
    (repo / "tests/test_new.py").write_text("", encoding="utf-8")
    errors = TOOL.check_repo(repo)
    assert any("test_files" in error and "过期" in error for error in errors)
    assert not any("「测试文件」" in error for error in errors)
    assert TOOL.main(["--root", str(repo)]) == 1


def test_figures_drift_reported_as_layer2_only(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(TOOL, "_run_git", _fake_git())
    TOOL.write_generated(repo, TOOL.collect_facts(repo, git_runner=_fake_git()))

    (repo / "docs/figures.md").write_text(_figures_md(test_files=3), encoding="utf-8")
    errors = TOOL.check_repo(repo)
    assert any("「测试文件」" in error for error in errors)
    assert not any("过期" in error for error in errors)  # live 未变，第 1 层仍绿
    assert TOOL.main(["--root", str(repo)]) == 1


def test_main_write_fails_loud_without_git(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(_root: Path, *_args: str) -> str:
        raise TOOL.FiguresError("未找到 git 可执行文件：无法计算 tracked_python_*（不静默输出 0）")

    monkeypatch.setattr(TOOL, "_run_git", _boom)
    assert TOOL.main(["--root", str(repo), "--write"]) == 1
    assert not (repo / TOOL.GENERATED_REL).exists()  # 失败不得留下半成品


# ---------------------------------------------------------------------------
# git 不可用必须显式失败（不静默输出 0）
# ---------------------------------------------------------------------------
def test_run_git_missing_binary_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_run(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(TOOL.subprocess, "run", _fake_run)
    with pytest.raises(TOOL.FiguresError, match="git"):
        TOOL._run_git(REPO_ROOT, "ls-files", "*.py")


def test_run_git_failure_raises_with_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_run(*_args: object, **_kwargs: object) -> None:
        raise subprocess.CalledProcessError(128, "git", stderr="fatal: not a git repository\n")

    monkeypatch.setattr(TOOL.subprocess, "run", _fake_run)
    with pytest.raises(TOOL.FiguresError, match="not a git repository"):
        TOOL._run_git(REPO_ROOT, "ls-files", "*.py")


def test_cli_exit_nonzero_without_git(tmp_path: Path) -> None:
    """端到端：非 git 目录上跑 CLI 必须 exit 1 且原因里带 git。"""
    root = tmp_path / "repo"
    _build_repo(root)
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(TOOL_PATH), "--root", str(root), "--check"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert result.returncode == 1
    combined = (result.stdout + result.stderr).lower()
    assert "git" in combined
