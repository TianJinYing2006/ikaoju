# 2026-11 重构批次执行报告

> 生成时间：2026-11-22（本地执行，未推送远端）
> 分支：`plan/2026-11-refactor`（基于 `dev@ff6f728`）
> 关联：2026-11 审阅意见中的「阶段 0~5」计划；ADR-0019。

## 0. 一句话结论

**阶段 0（止血）全部完成；阶段 1 的 figures 自动校验、独立裁判脚手架与确定性回放完成，评测 v2 实跑未完成；阶段 2 完成了 RunContext / 节点写回 / 评测并发隔离 / RAG 作用域显式化，类级 LLM 计数与其余 contextvar 去除未做；阶段 3 后端拆分完成第一刀（`api/meta.py` 薄路由层）与三处安全加固，完整拆分未做；阶段 4 完成仓库卫生、coverage 门禁、Windows 沙箱 job 与文档结构收敛（单页状态页），pip-audit/mypy 未加；阶段 5 完成 README 重写与本报告。** 计划中依赖外部条件（API key 跑评测、真实浏览器截图/录屏、GitHub runner 验证、生产部署权限）的项未完成，均如实登记在 §3。

全量测试：**1527 收集 / 1446 通过 / 81 跳过 / 0 失败**（Python 3.13 + `.deps`，2026-11-22 本机）。
6 个仓库守卫 + `tools/gen_figures.py --check` 全部通过。

## 1. 已完成项（按提交）

| 提交 | 内容 | 验收 |
| --- | --- | --- |
| `25d86a4` | **eval 并发隔离**：`_run_one(graph=None)` 在每个运行线程内新建图实例，Phase 1 不再共享同一 graph/Agent 状态 | `tests/test_eval_per_run_isolation.py`（3 例，含并发实例-线程一一对应断言） |
| `cbd2598` | **HITL token 计账**：`_clarify` / `_revise_plan` 写回 `token_used`，预算硬闸对 HITL 路径可见 | `tests/test_hitl_token_accounting.py`（4 例） |
| `db2a454` | **压缩降级可归因**：`ContextManager.compress` 失败进 `DegradationSink`，`_write` 节点 drain 进 `state.degradation_log`，不再静默 success | `tests/test_context_compression.py` 新增 2 例（含 graph 级） |
| `1cd15f1` | **validator fail-open 修复**：无事实句时 `citation_coverage = None`（原 `1.0`）；消费方已按 None 处理 | `tests/test_audit_p1_remediation.py` 新增 1 例 |
| `f5f939e` | **引擎架构收敛**：新增 `research_engine/runtime/`（`RunContext` + `new_run_context` + `with_token_writeback`）；12 个节点统一套写回装饰器并删除逐节点手写；`run()/iter_run()` 经 RunContext 归一取消谓词 | `tests/test_run_context.py`（12 例，含「全部生产节点已装饰」结构断言） |
| `a6d34d2` | **figures 机器校验**：`tools/gen_figures.py`（`--write/--check`，两层：live→JSON 新鲜度、JSON→figures.md 一致性）+ CI step | `tests/test_gen_figures.py`（22 例）；`--check` exit 0 |
| `8cc3f14` | figures 结构事实快照（`docs/figures.generated.json`） | `--check` 全 OK |
| `90ebc09` | **后端加固**：① `retry_run` 复用 `_check_input_safety`（审核/注入/空主题，此前绕过）② `_audit` 失败写 ERROR 日志（不再静默丢审计）③ Passkey 在 staging/production 未显式配置时 fail-closed（原 localhost 默认值导致 `enabled()` 恒真） | `tests/test_runs_management_api.py` +2；`tests/test_passkeys.py` +3 |
| `17f3c6b` | **ADR-0019**：RunContext / 节点写回 / per-run 隔离的背景、方案、取舍、影响；figures ADR 计数 18→19 | `check_doc_consistency` ADR 索引 OK |
| `7b11af2` | **仓库卫生**：`icon-lab/`（44 文件）、`ppt-report/`（33 文件）移出 git 跟踪（本地文件保留），`.tmp/` 忽略 | `gen_figures --check`；`git status` 干净（除用户本地未跟踪目录） |
| `（本次）` | **后端拆分第一刀**：`help`/`legal` 只读路由 → `web/backend/api/meta.py`（`include_router` 装配，路径/行为不变），建立 `api/` 薄路由层模式；main.py 减少约 55 行 | `tests/test_moderation_api.py`、`tests/test_web_guardrails.py`、`tests/test_auth_api.py` 全绿 |
| `（本轮）` | **独立裁判脚手架**：新增 `config.eval`（`DR_EVAL_JUDGE_MODEL` / `DR_EVAL_JUDGE_FAMILY`）、`judge_model_name` 优先读显式配置、`citation_judge_is_independent()` 动态判定、eval CLI `--judge-model/--judge-family` | `tests/test_eval_judge_scaffold.py`（6 例）+ 原 provenance 测试全绿 |
| `（本轮）` | **CI 质量门禁**：3.13 档 coverage ≥73%（实测 75.01%）、`security-windows` job（Windows 专属沙箱安全路径）；`pytest-cov` 进 dev 依赖与 lock | 本机 coverage 命令 exit 0；Windows 上 `test_code_exec` 29 例 + `test_arm2_code_injection` 39 例全过；CI YAML `yaml.safe_load` 通过 |
| `（本轮）` | **确定性回放**：`research_engine/search/replay.py`（录制 fixture → 零网络回放、未录制 query 可归因、坏 fixture fail-fast）；factory `SEARCH_PROVIDER=replay`；eval CLI `--replay-dir`（同时关 arXiv） | `tests/test_search_replay.py` 14 例（含「失败不泄漏 config 覆盖」回归） |
| `（本轮）` | **文档结构收敛**：新增单页 `docs/status/current.md`（能做什么/进行中/不宣称什么）；`docs/project-status.md` 降级为「历史长日志」并加归档横幅 | `check_md_tables` / `check_doc_consistency` 全过 |
| `（本轮）` | **contextvar 隐式依赖（RAG 作用域）显式化**：`create_graph(rag_scope=...)` / `Researcher(rag_scope=...)`；runner/worker 通过 `accepts_rag_scope` 能力标记显式注入，不再依赖「先 set_scope 再建图」的顺序；contextvar 仅作库函数/测试兜底 | `tests/test_rag_scope.py` 新增 3 例（显式优先于 contextvar、factory 透传、能力标记）；worker/loadtest 回归全绿 |
| （本报告） | 文档：README 重写、执行报告 | `check_doc_consistency` 6/6 OK |

另外：

- **README 重写**：从 531 行收敛为约 190 行；明确「是什么/不是什么」、三个核心设计、可复现的评测诚实口径
  （43.3% / 52.2% / 78.3% / 85.4% 四个口径并列、不可比较）、已知边界、文档地图；
  「单元测试 / E2E」数字保持与 figures 机器一致。
- **文档数字校正**：figures 的代码规模、pytest 收集、ADR 计数更新到今天实测口径（采集命令、日期一并更新）。

## 2. 计划 vs 实际（差异表）

| 计划项 | 状态 | 说明 |
| --- | --- | --- |
| 阶段 0：4 个矛盾缺口修复 + 回归测试 | ✅ 完成 | 4/4，均带回归测试 |
| 阶段 0：README/文档数字校正 | ✅ 完成 | README 重写 + figures 回填 |
| 阶段 1：figures 自动生成 + CI 守卫 | ✅ 完成 | 两层校验；结构事实（ADR/迁移/需求/测试文件/守卫清单/代码规模）机器校验 |
| 阶段 1：评测 v2（固定证据池回放、跨模型族独立裁判、人工抽检、RAG 评测集） | ⚠️ 部分 | **脚手架已完成**：独立裁判配置/provenance/CLI + **网络检索确定性回放**（`SEARCH_PROVIDER=replay` / `--replay-dir`，含 14 例测试）；**实跑未做**：需要真实 API key 与预算，arXiv/RAG 的 replay 仍待做 |
| 阶段 2：RunContext / per-run 隔离 / 节点写回 / 消除类级计数与 contextvar | ⚠️ 部分 | RunContext + 写回 + per-run 已完成；**RAG 检索作用域已显式化**（`create_graph(rag_scope=...)`，runner/worker 不再依赖 set_scope 顺序）；**类级 LLM 计数与 runtime_profile/usage contextvar 仍是现状**（需按模块分批显式化） |
| 阶段 3：后端拆分（app factory + routers + services + repositories） | ⚠️ 部分 | 完成**第一刀**：`help`/`legal` 只读路由拆到 `web/backend/api/meta.py`（`include_router` 装配，行为/路径不变），建立 `web/backend/api/` 薄路由层的模式；外加三处安全加固。`main.py` 仍 3,8xx 行，完整拆分方案与顺序见 §3 第 2 条 |
| 阶段 4：coverage / mypy / pip-audit / Windows job / npm devDeps | ⚠️ 部分 | **coverage 门禁（3.13 档 ≥73%，实测 75%）与 Windows 专属安全测试 job 已接入**（本机 Windows 实测 `test_code_exec` 29 例 + `test_arm2_code_injection` 39 例全过）；pip-audit / mypy / npm devDeps 未做 |
| 阶段 4：icon-lab / ppt-report 移出、github 元数据 | ⚠️ 部分 | 仓库卫生完成；GitHub description/homepage 需远端权限，未改 |
| 阶段 4：文档结构收敛 | ✅ 完成 | 新增 `docs/status/current.md` 单页状态；`docs/project-status.md` 转为历史长日志（加归档横幅与指针） |
| 阶段 5：README 重写 + 截图 + 90s demo | ⚠️ 部分 | README 完成；截图/录屏未做（需构建前端 + 真实浏览器会话，未在本晚执行；`DR_DEMO=1` 路径可复现） |
| 发布 v0.3.0 / 合并 dev / 推送 | ❌ 未做 | 明确不推送、不合入（遵守仓库「禁止直推 dev」纪律，且需人工 review） |

## 3. 未完成项与原因（如实登记）

1. **评测 v2 实跑**：需要调用外部模型产生真实数据；本机无 API key，且仓库纪律要求「先冻结代码/配置/数据集再跑基线」。
   强行用假数据产出「新基线」会污染 figures 的可信度，因此只完成**脚手架**（显式裁判配置 + provenance 动态独立性判定 + CLI + 测试），不产出数字。
   拿到 key 后按「固定证据池回放 + 跨模型族裁判 + 50 条人工抽检 + RAG 20 题集」执行。
2. **后端拆分**：`web/backend/main.py` 3,846 行、66 个路由，与 `store`/`manager` 等模块级单例深度耦合；
   一次机械搬移的回归面不可控。建议按域分 6 个 PR（auth → runs/stream → rag → export/share → ops/meta → worker/runner），
   每 PR 先建 `web/backend/api/<domain>.py` 薄 router，再加契约测试。
3. **覆盖率/类型/依赖扫描**：coverage 门禁（73%）与 Windows 安全 job 已接入；剩余 **pip-audit**（需先接受存量告警策略）
   与 **mypy**（对 6.8 万行全量必然大面积报错，建议只对 `research_engine/runtime/`、`web/backend/api/` 新包增量生效）。
4. **截图/录屏**：需要 `npm ci && npm run build` 后用 Playwright 截 4 个真实页面；本晚优先保证代码与测试，
   该产物留待有浏览器环境时补。
5. **Windows 沙箱测试进 CI**：GitHub-hosted runner 有 windows-latest，但当前 workflow 未配置；
   加入需新建 job 并验证 15 分钟内完成。

## 4. 下一步建议（按投入产出）

1. **补评测 v2**（1~2 天，需 API key）：固定证据池回放 + 跨模型族裁判 + 50 条人工标注 + RAG 20 题集；
   重采一次基线后，把 figures 的「引用准确率」从多口径并列改为「当前口径 + 区间」。
2. **后端拆分**（1~2 周）：按 §3.2 的 6 个 PR 顺序；先抽 `web/backend/api/meta.py`（legal/help/feedback，无状态耦合）做样板。
3. **剩余 CI 质量门禁**（半天）：pip-audit + mypy 增量（coverage 与 Windows job 已完成）。
4. **呈现补齐**（半天）：4 张截图 + 90 秒 demo；GitHub description/homepage。
5. **剩余 contextvar/类级计数收敛**（2~3 天）：在每个域拆分 PR 中顺带改为显式依赖，不做一次性大爆炸。

## 5. 合并 / 回滚方式

```bash
# 查看本批次
git log --oneline dev..plan/2026-11-refactor
git diff --stat dev..plan/2026-11-refactor

# 合并（按仓库纪律应先 PR + 人工 review；本地验证后可 fast-forward）
git checkout dev && git merge --ff-only plan/2026-11-refactor

# 回滚任一批次（示例：撤销仓库卫生提交）
git revert 7b11af2
# 恢复 icon-lab/ppt-report 跟踪（文件仍在本地）：
git checkout dev -- icon-lab ppt-report
```

## 6. 本批次已验证的证据

```text
py -3.13 -m pytest tests/ --tb=no
→ 1446 passed, 81 skipped, 6 warnings

py -3.13 tools/check_doc_consistency.py
→ 6 项 [OK]（figures tag / ICP / CI 守卫清单 / 开放注册口径 / ADR 索引 / README 数字 == figures）

py -3.13 tools/gen_figures.py --check
→ 结构事实全部 [OK]（ADR 19 / 需求 45 / 迁移 31+31 / 测试文件 160 / Python 320 文件 68,912 行 / 守卫 6）

py -3.13 -m pytest tests/ --tb=no -q --cov=research_engine --cov=web --cov-fail-under=73
→ 1467 passed, 81 skipped；coverage 75.01%，Required test coverage of 73% reached

Windows 本机：py -3.13 -m pytest tests/test_code_exec.py tests/test_arm2_code_injection.py -q
→ 29 + 39 passed（对应 CI security-windows job）
```
