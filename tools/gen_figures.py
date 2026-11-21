#!/usr/bin/env python3
"""对外数字中「可机器判定」的结构事实：生成器 + 两层守卫（2026-11-22 建立）。

为什么存在：`docs/figures.md` 是对外数字的唯一真相源（`CONTRIBUTING.md` §0 / §13），
但其中多行是**手工维护的结构计数**（ADR / 需求文档 / 数据库迁移 / 测试文件 / CI 守卫），
每次批次收口都靠人肉同步，已多次出现「改了 A 没同步 B」式的漂移。本工具把其中
**确定性可算**的部分收敛为生成物 `docs/figures.generated.json`，并在 CI 里比对。

设计纪律（与 `tools/check_doc_consistency.py` 同口径，防「永远绿的摆设」）：
- 必需目录 / 必需表格行缺失 = **失败**，不跳过；
- git 不可用 = **失败**并给出清晰原因，绝不静默输出 0；
- 零网络、零 LLM、确定性输出（无时间戳；JSON `sort_keys` + UTF-8 + 末尾换行）。

两层校验（并发批次互不阻塞的关键）：
1. **live -> JSON 新鲜度**：`--write` 之后仓库结构变了（新增 ADR / 测试文件 / 迁移等），
   `--check` 报「generated JSON stale vs live」，提示重跑 `--write`；
2. **JSON -> figures.md 一致性**：`docs/figures.md` 的表格行必须与**已提交 JSON** 一致。
   因此并发改动只会命中相应层，错误信息能直接指出该改 JSON 还是该改 figures.md。

用法：
    python tools/gen_figures.py --write    # 生成 docs/figures.generated.json
    python tools/gen_figures.py --check    # 默认行为；两层校验，失败退出码 1
    python tools/gen_figures.py --root <repo> --check
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATED_REL = Path("docs") / "figures.generated.json"

# 与 tools/check_doc_consistency.py 完全相同的正则：CI workflow 实际调用的守卫脚本
CI_GUARD_SCRIPT_RE = re.compile(r"tools/(check_[a-z0-9_]+\.py)")
# 行内代码里的 `|` 不是表格分隔符（与 tools/check_md_tables.py 同口径）
_CELL_SPLIT = re.compile(r"(?<!\\)\|")
_INLINE_CODE = re.compile(r"`[^`\n]*`")

# figures.md §1/§2/§3 中与生成事实对应的行标签（缺失 = 失败，不跳过）
FIGURE_ROW_LABELS = ("ADR", "需求文档", "数据库迁移", "测试文件", "CI 守卫")

# git 事实（无法用纯文件系统计算；git 不可用必须显式失败）
GitRunner = Callable[..., str]


class FiguresError(RuntimeError):
    """生成/校验自身无法继续的确定性失败（缺目录 / git 不可用 / JSON 损坏）。"""


# ---------------------------------------------------------------------------
# 文件系统事实
# ---------------------------------------------------------------------------
def _require_dir(root: Path, rel: str) -> Path:
    path = root / rel
    if not path.is_dir():
        raise FiguresError(f"缺少必需目录：{rel}/（--root 是否指向仓库根？）")
    return path


def _count_files(directory: Path, pattern: str, *, exclude: tuple[str, ...] = ()) -> int:
    return sum(
        1 for path in directory.glob(pattern) if path.is_file() and path.name not in exclude
    )


def _read_text(path: Path, *, source: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FiguresError(f"缺少必需文件：{source}") from exc
    except UnicodeDecodeError as exc:
        raise FiguresError(f"文件不是 UTF-8 编码：{source}") from exc


def _ci_guard_script_files(workflow_dir: Path) -> list[str]:
    """CI workflow 实际调用的去重守卫脚本名（注释行不算）。"""
    names: set[str] = set()
    for path in sorted(workflow_dir.glob("*.yml")):
        if not path.is_file():
            continue
        text = _read_text(path, source=f".github/workflows/{path.name}")
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            names.update(CI_GUARD_SCRIPT_RE.findall(line))
    if not names:
        # 零守卫不是「0 个」也不是可接受状态：守卫清单事实会变成空转
        raise FiguresError("CI workflow 未找到任何 tools/check_*.py 调用（守卫清单事实无法计算）")
    return sorted(names)


# ---------------------------------------------------------------------------
# git 事实
# ---------------------------------------------------------------------------
def _run_git(root: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
    except FileNotFoundError as exc:
        raise FiguresError(
            "未找到 git 可执行文件：无法计算 tracked_python_files/tracked_python_lines"
            "（本工具不静默输出 0；请安装 git 或改用能提供 git 元数据的环境）"
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip().splitlines()[:1]
        raise FiguresError(
            f"git {' '.join(args)} 失败：{detail}"
            "（无法计算 tracked_python_*，不静默输出 0；--root 必须指向 git 仓库根）"
        ) from exc
    return proc.stdout


def _tracked_python_stats(root: Path, git_runner: GitRunner) -> tuple[list[str], int]:
    """`git ls-files '*.py'` 的文件清单与总行数（逐文件 UTF-8 读取，物理行）。"""
    output = git_runner(root, "ls-files", "*.py")
    files = sorted({line.strip() for line in output.splitlines() if line.strip()})
    total_lines = 0
    for rel in files:
        path = root / rel
        if not path.is_file():
            raise FiguresError(f"git 跟踪的 Python 文件在工作区缺失：{rel}（先恢复文件或更新索引）")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise FiguresError(f"git 跟踪的 Python 文件不是 UTF-8：{rel}") from exc
        total_lines += len(text.splitlines())
    return files, total_lines


# ---------------------------------------------------------------------------
# 事实采集 / 生成物
# ---------------------------------------------------------------------------
def collect_facts(repo_root: Path, *, git_runner: GitRunner | None = None) -> dict[str, object]:
    """采集确定性结构事实。`git_runner` 仅用于单测注入（默认走真实 git）。"""
    root = Path(repo_root)
    if not root.is_dir():
        raise FiguresError(f"仓库根目录不存在：{root}")

    adr_count = _count_files(
        _require_dir(root, "docs/decisions"), "0[0-9][0-9][0-9]-*.md"
    )
    requirement_docs = _count_files(
        _require_dir(root, "docs/requirements"), "*.md", exclude=("模板.md",)
    )
    migrations = _count_files(_require_dir(root, "migrations"), "*.sql")
    migration_checks = _count_files(_require_dir(root, "migrations/checks"), "*.sql")
    test_files = _count_files(_require_dir(root, "tests"), "test_*.py")
    e2e_spec_files = _count_files(_require_dir(root, "web/frontend/e2e"), "*.spec.ts")
    guard_files = _ci_guard_script_files(_require_dir(root, ".github/workflows"))

    runner = git_runner if git_runner is not None else _run_git
    tracked_files, tracked_lines = _tracked_python_stats(root, runner)

    return {
        "adr_count": adr_count,
        "ci_guard_script_files": guard_files,
        "ci_guard_scripts": len(guard_files),
        "e2e_spec_files": e2e_spec_files,
        "migration_checks": migration_checks,
        "migrations": migrations,
        "requirement_docs": requirement_docs,
        "test_files": test_files,
        "tracked_python_files": len(tracked_files),
        "tracked_python_lines": tracked_lines,
    }


def render_generated(facts: dict[str, object]) -> str:
    """确定性 JSON 文本：排序键、UTF-8、缩进 2、末尾换行、无时间戳。"""
    return json.dumps(facts, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_generated(repo_root: Path, facts: dict[str, object]) -> Path:
    path = Path(repo_root) / GENERATED_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    # 直接写字节：避免 Windows 文本模式的 CRLF 让生成物跨平台不一致
    path.write_bytes(render_generated(facts).encode("utf-8"))
    return path


def load_generated(repo_root: Path) -> dict[str, object]:
    path = Path(repo_root) / GENERATED_REL
    if not path.is_file():
        raise FiguresError(
            f"缺少 {GENERATED_REL.as_posix()}：先运行 python tools/gen_figures.py --write"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise FiguresError(f"{GENERATED_REL.as_posix()} 不是 UTF-8 编码") from exc
    except json.JSONDecodeError as exc:
        raise FiguresError(f"{GENERATED_REL.as_posix()} 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise FiguresError(f"{GENERATED_REL.as_posix()} 顶层必须是 JSON 对象（当前 {type(data).__name__}）")
    return data


# ---------------------------------------------------------------------------
# 第 2 层：JSON -> figures.md
# ---------------------------------------------------------------------------
def _table_rows(md_text: str) -> list[list[str]]:
    """返回所有表格行的单元格列表（首列 = 标签列）。"""
    rows: list[list[str]] = []
    for line in md_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = _INLINE_CODE.sub("CODE", stripped)
        if cells.startswith("|"):
            cells = cells[1:]
        if cells.endswith("|"):
            cells = cells[:-1]
        rows.append([c.strip() for c in _CELL_SPLIT.split(cells)])
    return rows


def _find_row(rows: list[list[str]], label: str) -> tuple[list[str] | None, list[str]]:
    hits = [cells for cells in rows if cells and cells[0] == label]
    if not hits:
        return None, [f"docs/figures.md 缺少必需表格行「{label}」（跨层校验不跳过）"]
    if len(hits) > 1:
        return None, [f"docs/figures.md 表格行「{label}」出现 {len(hits)} 次（应唯一）"]
    return hits[0], []


def _leading_number(value: str) -> int | None:
    """取「当前值」列开头的数字：优先加粗 `**N**`，否则第一个 `N`。"""
    match = re.search(r"\*\*(\d+)", value) or re.search(r"(\d+)", value)
    return int(match.group(1)) if match else None


def _row_number(
    rows: list[list[str]], label: str
) -> tuple[int | None, list[str], list[str] | None]:
    cells, errors = _find_row(rows, label)
    if cells is None:
        return None, errors, None
    if len(cells) < 2:
        return None, [f"docs/figures.md「{label}」行缺少取值列"], cells
    number = _leading_number(cells[1])
    if number is None:
        return None, [f"docs/figures.md「{label}」行缺少数字计数（当前值列：{cells[1]!r}）"], cells
    return number, [], cells


def _check_count(
    rows: list[list[str]], label: str, key: str, facts: dict[str, object]
) -> list[str]:
    documented, errors, _cells = _row_number(rows, label)
    if documented is None:
        return errors
    actual = facts.get(key)
    if actual != documented:
        return [
            f"docs/figures.md「{label}」= {documented}，生成值 {key} = {actual}（不一致；"
            "先 --write 再同步 figures.md）"
        ]
    return []


def check_against_figures(facts: dict[str, object], figures_md_text: str) -> list[str]:
    """第 2 层校验：生成事实 vs `docs/figures.md` 表格行（只读已提交 JSON 的事实）。"""
    rows = _table_rows(figures_md_text)
    errors: list[str] = []
    errors.extend(_check_count(rows, "ADR", "adr_count", facts))
    errors.extend(_check_count(rows, "需求文档", "requirement_docs", facts))
    errors.extend(_check_count(rows, "测试文件", "test_files", facts))
    errors.extend(_check_count(rows, "CI 守卫", "ci_guard_scripts", facts))

    migrations, row_errors, cells = _row_number(rows, "数据库迁移")
    errors.extend(row_errors)
    if migrations is not None and cells is not None:
        expected_migrations = facts.get("migrations")
        if expected_migrations != migrations:
            errors.append(
                f"docs/figures.md「数据库迁移」= {migrations}，生成值 migrations = {expected_migrations}"
                "（不一致；先 --write 再同步 figures.md）"
            )
        check_match = re.search(r"(\d+)\s*个结构断言", " ".join(cells[1:]))
        if check_match is None:
            errors.append(
                "docs/figures.md「数据库迁移」行缺少「N 个结构断言」计数"
                "（migration_checks 无法校验；只前向迁移必须同步结构断言）"
            )
        elif int(check_match.group(1)) != facts.get("migration_checks"):
            errors.append(
                f"docs/figures.md「数据库迁移」结构断言 = {check_match.group(1)}，"
                f"生成值 migration_checks = {facts.get('migration_checks')}（不一致）"
            )
    return errors


# ---------------------------------------------------------------------------
# 第 1 层：live -> JSON
# ---------------------------------------------------------------------------
def compare_generated(live: dict[str, object], generated: dict[str, object]) -> list[str]:
    errors: list[str] = []
    for key in sorted(set(live) | set(generated)):
        if key not in generated:
            errors.append(
                f"{GENERATED_REL.as_posix()} 缺少字段 {key}（live={json.dumps(live[key], ensure_ascii=False)}）"
            )
        elif key not in live:
            errors.append(
                f"{GENERATED_REL.as_posix()} 含未知字段 {key}="
                f"{json.dumps(generated[key], ensure_ascii=False)}（本工具不生成该字段，不静默忽略）"
            )
        elif generated[key] != live[key]:
            errors.append(
                f"{GENERATED_REL.as_posix()} 过期（stale vs live）：{key} "
                f"JSON={json.dumps(generated[key], ensure_ascii=False)} "
                f"live={json.dumps(live[key], ensure_ascii=False)}"
                "（先运行 python tools/gen_figures.py --write）"
            )
    return errors


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def check_repo(repo_root: Path, *, git_runner: GitRunner | None = None) -> list[str]:
    """执行两层校验并打印逐项 OK/FAIL，返回错误列表（空 = 通过）。"""
    root = Path(repo_root)
    errors: list[str] = []
    live: dict[str, object] | None = None
    generated: dict[str, object] | None = None

    try:
        live = collect_facts(root, git_runner=git_runner)
    except FiguresError as exc:
        print(f"[FAIL] live facts：{exc}")
        errors.append(f"无法采集 live facts：{exc}")

    try:
        generated = load_generated(root)
    except FiguresError as exc:
        print(f"[FAIL] {GENERATED_REL.as_posix()}：{exc}")
        errors.append(str(exc))

    if live is not None and generated is not None:
        for key in sorted(set(live) | set(generated)):
            if key in live and key in generated and live[key] == generated[key]:
                print(f"[OK] {key} = {json.dumps(live[key], ensure_ascii=False)}")
            elif key not in live:
                print(f"[FAIL] {key}：JSON 含未知字段 {json.dumps(generated[key], ensure_ascii=False)}")
            elif key not in generated:
                print(
                    f"[FAIL] {key}：JSON 缺少字段（live={json.dumps(live[key], ensure_ascii=False)}）"
                )
            else:
                print(
                    f"[FAIL] {key}：JSON={json.dumps(generated[key], ensure_ascii=False)} "
                    f"live={json.dumps(live[key], ensure_ascii=False)}"
                )
        errors.extend(compare_generated(live, generated))

    if generated is not None:
        figures_path = root / "docs" / "figures.md"
        if not figures_path.is_file():
            print("[FAIL] docs/figures.md：缺少必需文件（跨层校验不跳过）")
            errors.append("缺少必需文件：docs/figures.md（跨层校验不跳过）")
        else:
            figures_text = figures_path.read_text(encoding="utf-8")
            figure_errors = check_against_figures(generated, figures_text)
            for label in FIGURE_ROW_LABELS:
                if any(f"「{label}」" in error for error in figure_errors):
                    print(f"[FAIL] figures.md「{label}」")
                else:
                    print(f"[OK] figures.md「{label}」")
            for error in figure_errors:
                print(f"   - {error}")
            errors.extend(figure_errors)

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="figures 结构事实生成器与两层守卫（零网络、零 LLM、确定性）"
    )
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="仓库根目录（默认本文件上一级）")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--write", action="store_true", help=f"生成 {GENERATED_REL.as_posix()}")
    group.add_argument("--check", action="store_true", help="校验生成物与 figures.md（默认行为）")
    args = parser.parse_args(argv)

    if args.write:
        try:
            facts = collect_facts(args.root)
        except FiguresError as exc:
            print(f"[FAIL] 无法生成 {GENERATED_REL.as_posix()}：{exc}")
            return 1
        write_generated(args.root, facts)
        print(f"[WRITE] {GENERATED_REL.as_posix()}（{len(facts)} 项事实，无时间戳）")
        for key in sorted(facts):
            print(f"   {key} = {json.dumps(facts[key], ensure_ascii=False)}")
        return 0

    errors = check_repo(args.root)
    print("---")
    if errors:
        print(f"figures 结构事实校验失败 {len(errors)} 处：修正后重跑 python tools/gen_figures.py --check")
        return 1
    print("figures 结构事实校验全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
