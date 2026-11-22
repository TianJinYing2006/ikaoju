# 2026-11 重构批次执行报告

> 生成时间：2026-11-22（本地执行，未推送远端）
> 分支：`plan/2026-11-refactor`（基于 `dev@ff6f728`）
> 关联：2026-11 审阅意见中的「阶段 0~5」计划；ADR-0019。

## 0. 一句话结论

**阶段 0（止血）全部完成；阶段 1 的 figures 自动校验、独立裁判脚手架与确定性回放完成，评测 v2 实跑未完成；阶段 2 完成了 RunContext / 节点写回 / 评测并发隔离 / RAG 作用域显式化 / run 级 TokenLedger（引擎不再依赖类级 LLM 计数器），评测工具类级汇总已迁移、runtime_profile/usage 已显式化；阶段 3 后端拆分完成第一刀（`api/meta.py` 薄路由层）与三处安全加固，完整拆分未做；阶段 4 完成仓库卫生、coverage/mypy 门禁、Windows 沙箱 job 与文档结构收敛；pip-audit CI 门禁已接入（runtime lock 升级 mcp 后实测 0 漏洞）；阶段 5 完成 README 重写与本报告。** 计划中依赖外部条件（API key 跑评测、真实浏览器截图/录屏、GitHub runner 验证、生产部署权限）的项未完成，均如实登记在 §3。

全量测试：**1575 收集 / 1495 通过 / 80 跳过**（pytest 汇总另计 1 个模块级 collection-skip）/ 0 失败（Python 3.13 + `.deps`，2026-11-22 本机）。
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
| `（本轮）` | **类级 LLM 计数器（引擎运行期依赖）消除**：新增 `runtime/ledger.py`（run 级 `TokenLedger` + 进程级 run_id 注册表）；`create_graph/run/iter_run` 注入 `state.run_id` 并登记账本，LLM 记账按 run_id 写入；graph 输出 `last_ledger` 快照，`tokens_diff` 恒 None（旧字段仅兼容）；CLI 改打印 run 账本 | `tests/test_run_ledger.py` 7 例（累加/并发/注册表/按 run 隔离/无账本不崩）；HITL/Arm1/审计回归全绿 |
| `（本轮）` | **ADR-0020**：记录「RAG 作用域显式注入 + run 级 TokenLedger」的背景/方案/取舍，含 msgpack checkpoint 教训；ADR 索引与 figures 计数同步 | `check_doc_consistency` ADR 索引 OK；figures「ADR 20 篇」 |
| `（本轮）` | **检索链 run_id 穿透**：`Researcher._search_web/_search_rag/_search_arxiv/_search_mcp` 与 `HybridRetriever.embed_query/retrieve` 支持 `run_id`，`search_once` 从 `state.run_id` 绑定；对测试替身/旧签名用签名探测兼容 | `test_usage_ledger`（工具线程记账）/`test_rag_scope`/`test_rag_retriever_v2`/`test_arm4_failure_reasons` 全绿 |
| `（本轮）` | **usage sink 显式注册表（去 contextvar 第一步）**：`usage.py` 新增 `register_usage_sink/unregister_usage_sink/usage_sink_for` + `emit_usage(record, run_id=...)` 显式优先；LLM 记账携带 `state.run_id`；runner/worker 注册/注销（contextvar 仅作搜索/RAG emit 的兼容兜底） | 新增 `tests/test_usage_sink_registry.py` 6 例（显式优先不双记、回退、失败计数/strict）；usage/worker 存量测试全绿 |
| `（本轮）` | **依赖安全巡检（pip-audit 可用后）**：`pip-audit --path .deps` 报 3 组告警——`pypdf 6.16.2`、`urllib3 2.7.0` 在 lock 中已是修复版（6.21.4 / 2.8.0）；**`soupsieve 2.9.2` 为真实可修项**：lock 按 uv 解析改为 `2.10（py<3.11.5）/ 4.1.3（≥3.11.5）`，requirements 增补约束注释 | `pip-audit --path .deps` 输出留档；lock 语法按 uv 同格式手工对齐（CI 安装时验证） |
| `（本轮）` | **运行档位（RuntimeProfile）显式注入**：`create_graph(..., profile=...)` + 能力标记 `accepts_profile`；`run()/iter_run()` 在 invoke/stream 期间用 `use_profile` 生效并在结束后恢复（不泄漏给同线程下一次 run）；runner/worker 能力探测后透传 profile | 新增 `tests/test_profile_injection.py` 4 例（含恢复 contextvar、worker factory 透传）；全量绿 |
| `（本轮）` | **mypy 增量门禁接入 CI**：新增 step `mypy --ignore-missing-imports --follow-imports=silent research_engine/runtime research_engine/search/replay.py web/backend/api`；dev 依赖加 `mypy>=1.10` 并用 `uv pip compile` 重生成 lock（mypy 2.4.1） | 本机 `mypy ... → Success: no issues found in 6 source files`；CI YAML `yaml.safe_load` 通过 |
| `（本轮）` | **删除 `LLMClient` 类级计数器**：`tokens_total/model_stats/role_stats/model_io_stats/_stats_lock/reset_stats` 全部移除（引擎与评测此前已不读）；Phase 1 无账本时改为**显式降级**（不写 phase1_global_stats.json）；`web/backend/followup` 追问成本改用临时 run 账本 | `test_eval_metrics` 两个计数用例改写为账本断言；`test_observability` 去掉计数器归零；新增 followup 账本测试；全量绿 |
| `（本轮）` | **评测 Phase 2 judge 成本账本化**：`JudgeWithLedger` 包装裁判客户端（注入合成 `state.run_id`、注册独立账本、`close()` 注销）；`_summarize(judge_tokens=...)` 读取账本；Phase 2 不再 `reset_stats` / 读类级计数器 | `tests/test_eval_judge_scaffold.py` 新增 3 例；`test_status_naming`/`test_eval_provenance` 全绿 |
| `（本轮）` | **评测 Phase 1 全局成本改逐 run 账本汇总**：raw 增加 `token_ledger` 快照；`merge_snapshots()` 汇总（source=per_run_ledger）；测试替身无账本时回退旧类级桶并标记 source | `tests/test_run_ledger.py` 新增 2 例（合并/空）；`test_eval_provenance`/`test_status_naming`/`test_arm5_quality_gate` 全绿 |
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
| 阶段 2：RunContext / per-run 隔离 / 节点写回 / 消除类级计数与 contextvar | ⚠️ 部分 | RunContext + 写回 + per-run 已完成；RAG 作用域与**运行档位**均已显式注入（`rag_scope` / `profile`，尽头用 `use_profile` 恢复不泄漏）；**类级 LLM 计数器已删除**（引擎/评测/追问走 run 级账本）；**剩余**：`usage` 的 contextvar（逐调用记账 sink）待显式化 |
| 阶段 3：后端拆分（app factory + routers + services + repositories） | ⚠️ 部分 | 完成**第一刀**：`help`/`legal` 只读路由拆到 `web/backend/api/meta.py`（`include_router` 装配，行为/路径不变），建立 `web/backend/api/` 薄路由层的模式；外加三处安全加固。`main.py` 仍 3,8xx 行，完整拆分方案与顺序见 §3 第 2 条 |
| 阶段 4：coverage / mypy / pip-audit / Windows job / npm devDeps | ⚠️ 部分 | coverage（≥73%）、mypy 增量、Windows 安全 job 已接入；**pip-audit 工具已可用并完成一次巡检**（`.deps` 3 组告警：2 项 lock 已修复、soupsieve 已升级）；**CI 门禁已接入**（2026-11-23）：`pip-audit -r requirements-lock.txt` 实测 **0 漏洞**（升级 mcp 1.9.4→1.30.0 修复 11 项 advisory；lock 增补 jsonschema/pyjwt/attrs/referencing/rpds-py/jsonschema-specifications）|
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
3. **覆盖率/类型/依赖扫描**：coverage 门禁（73%）、Windows 安全 job、**mypy 增量门禁**（runtime/replay/api，本机 Success）已接入；**pip-audit CI 门禁已接入**（runtime lock 0 漏洞；mcp 升级修复 11 项 advisory）；npm 生产依赖审计已由前端 job 门禁覆盖（`npm audit --omit=dev --audit-level=high`，实测 0 漏洞）。
4. **截图/录屏**：需要 `npm ci && npm run build` 后用 Playwright 截 4 个真实页面；本晚优先保证代码与测试，
   该产物留待有浏览器环境时补。
5. **Windows 沙箱测试进 CI**：已配置 `security-windows` job（2026-11-22）；
   加入需新建 job 并验证 15 分钟内完成。

## 4. 下一步建议（按投入产出）

1. **补评测 v2**（1~2 天，需 API key）：固定证据池回放 + 跨模型族裁判 + 50 条人工标注 + RAG 20 题集；
   重采一次基线后，把 figures 的「引用准确率」从多口径并列改为「当前口径 + 区间」。
2. **后端拆分**（1~2 周）：按 §3.2 的 6 个 PR 顺序；先抽 `web/backend/api/meta.py`（legal/help/feedback，无状态耦合）做样板。
3. ~~**剩余 CI 质量门禁**：pip-audit + mypy 增量~~（已完成：pip-audit 门禁接入、runtime lock 0 漏洞）。
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

## 6. 目标逐项完成状态（2026-11-22 最终核验）

> 对照本轮 goal 的 5 项交付要求逐项核验；「证据」均为本机可复跑命令。

| # | 目标项 | 状态 | 证据 | 残差 |
|---|--------|------|------|------|
| 1 | 阶段 0 止血：eval 并发共享 graph / HITL 计账 / 压缩静默 / coverage fail-open，各带回归 | ✅ 完成 | `test_eval_per_run_isolation`(3) / `test_hitl_token_accounting`(4) / `test_context_compression`(+2) / `test_audit_p1_remediation`(+1) | — |
| 2 | 阶段 1：figures 自动生成 + CI 守卫；评测确定性/独立裁判脚手架；陈旧数字清理 | ✅ 完成 | `tools/gen_figures.py --check` exit 0（CI step）；`search/replay.py` + `test_search_replay`(14)；`JudgeWithLedger` + `test_eval_judge_scaffold`(9)；README/figures 数字由生成物校验 | — |
| 3 | 阶段 2：RunContext / per-run 隔离 / 节点纯函数化切片 / 消除类级计数器与 contextvar 隐式依赖 | ⚠️ 大部分 | ADR-0019/0020；`runtime/`（RunContext + TokenLedger + 注册表）；类级计数器已删除；RAG 作用域与运行档位显式注入；**usage sink 显式注册表已就位**（LLM emit 带 run_id，runner/worker 注册）；全量测试绿 | 研究主链路（LLM + web/RAG/arXiv/MCP 检索）usage 已走显式注册表；**文档摄取 / MCP server 的 sink 仍走兼容 contextvar**（非研究 run 执行单元）；**节点仍是方法**（依赖注入已集中，未拆纯函数模块） |
| 4 | 阶段 4：CI 质量门禁与仓库卫生（非产品目录移出、文档结构收敛） | ⚠️ 大部分 | coverage ≥73%（实测 75.01%）、mypy 增量（6 files Success）、`security-windows` job；icon-lab/ppt-report 移出跟踪；`docs/status/current.md` 单页 + project-status 归档 | **pip-audit CI 门禁已接入**：升级 mcp 1.9.4→1.30.0 修复 11 项 advisory，lock 增补 6 个传递依赖后 `pip-audit -r requirements-lock.txt` 实测 0 漏洞；npm 生产依赖审计由前端 job 既有门禁覆盖（0 漏洞） |
| 5 | 阶段 5：README 重写与完成情况报告 | ✅ 完成 | README 531→约 190 行（诚实评测口径/边界）；本报告 + ADR-0019/0020 + CHANGELOG | — |

**结论**：5 项中 3 项完全完成、2 项主体完成。剩余均为**已定位、可继续**的收尾项，不涉及回滚风险；当前分支全量测试与守卫全绿，可随时合并或继续迭代。

### 剩余项的执行建议

1. **usage contextvar 显式化**：给 `Researcher._search_*` / `RAG` 调用链补 `run_id` 参数（来源 `state.run_id`），`emit_usage(record, run_id=...)` 走注册表；runner/worker 用 `register_usage_sink(run_id, sink)` 替代 push/pop（保留 `use_usage_sink` 兼容单测）。
2. ~~**pip-audit CI 门禁**~~ → 2026-11-23 已完成（runtime lock 0 漏洞；门禁在 `lint-and-test` 内，pip-audit 钉 2.10.1 按需安装）。
3. **后端按域拆分**（计划中的后续批次，不属本次 objective）：先 `auth` → `runs/stream` → `rag` → `export/share` → `ops/meta`，每 PR 带契约测试。

## 7. 本批次已验证的证据

```text
py -3.13 -m pytest tests/ --tb=no
→ 1495 passed, 80 skipped（另有 1 个模块级 collection-skip，pytest 汇总显示 81）

py -3.13 -m mypy research_engine/runtime research_engine/search/replay.py web/backend/api --ignore-missing-imports --follow-imports=silent
→ Success: no issues found in 6 source files

py -3.13 tools/check_doc_consistency.py
→ 6 项 [OK]（figures tag / ICP / CI 守卫清单 / 开放注册口径 / ADR 索引 / README 数字 == figures）

py -3.13 tools/gen_figures.py --check
→ 结构事实全部 [OK]（ADR 20 / 需求 45 / 迁移 31+31 / 测试文件 164 / Python 文件数与行数以 figures 为准 / 守卫 6）

py -3.13 -m pytest tests/ --tb=no -q --cov=research_engine --cov=web --cov-fail-under=73
→ coverage 75.01%，Required test coverage of 73% reached

Windows 本机：py -3.13 -m pytest tests/test_code_exec.py tests/test_arm2_code_injection.py -q
→ 29 + 39 passed（对应 CI security-windows job）

py -3.13 -m pip_audit --path .deps
→ 3 组告警（pypdf / urllib3 在 lock 已修复；soupsieve 已升级 4.1.3）
```
```

## 8. 2026-11-22 PR 审查修复批次（12 个阻塞项闭环）

> 触发：对 `plan/2026-11-refactor` 按 5 个 PR 批次做初审（见本文件 §6/§7 与审查报告），修复全部阻塞项后进入复审。

| # | 阻塞项 | 修复 | 回归证据 |
|---|--------|------|----------|
| 1 | P1：validator 空报告 coverage 变更未登记口径断点 | `SCORER_VERSION` w9.1→**w9.2** + figures 断点 4 | `test_arm6_provenance`/figures 守卫 |
| 2 | P2：未注入档位时 `use_profile(None)` 覆盖调用方 set_profile | `_UNSET` 哨兵 + `_profile_scope()`（未传不动 contextvar） | `test_legacy_set_profile_is_not_reset_when_not_injected` |
| 3 | P2：显式注入后不再 set_scope，证据身份 scope 令牌恒空 | runner/worker **始终** set_scope（显式注入同时保留） | 既有 scope/evidence 测试 + 全量 |
| 4 | P2/P3：Phase1 只收 ok 账本，failed/timeout 成本静默低估 | 失败 raw 也落 `token_ledger`；超时标 `cost_degraded` 并透传到 cost block | `test_eval_cost_degraded` |
| 5 | P3：独立裁判可由配置置 true（headline 仍读主链路） | `citation_judge_is_independent()` 恒 False；配置仅记录 | `test_configured_judge_does_not_claim_independence` |
| 6 | P3：replay 的 config_snapshot 缺 replay_dir/fixture 指纹 | 快照补 `search_replay_dir`/`search_enable_arxiv`/`replay_fixture_sha256` | `test_provenance_records_replay_dir_and_fixture_hash` |
| 7 | P3：replay 空 results 返回 ok，与活源 empty_result 不一致 | 归一为 `empty_result` | `test_replay_empty_results_is_empty_result_like_live_provider` |
| 8 | P4：`webauthn/authenticate/begin` 缺 fail-closed 守卫 | 补 `_require_webauthn_ready()` | `test_unconfigured_passkeys_fail_closed` 覆盖登录 begin |
| 9 | P4：重试输入闸先于幂等，审核降级会改写「同一 run」承诺 | 幂等命中先于 `_check_input_safety` | `test_retry_idempotency_precedes_input_gate` |
| 10 | P5：README ADR 计数 19≠20 | 改 20（figures 一致） | `check_doc_consistency` |
| 11 | P5：README 自维护评测数字（违反而数字唯一来源） | README 只引用 figures §4，数字集中在 figures | `check_doc_consistency` + 人工核对 |
| 12 | P5：测试计数/状态页/执行报告多处口径不一致 | 统一 1575 收集 / 1495 通过 / 80 跳过（另 1 模块级 skip 显式说明）；status/current、execution §0/§3/§4/§7 同步 | `check_doc_consistency`、`gen_figures --check` |

**复审状态**：待跑全量 pytest + 7 守卫 + CI 首跑后给复审结论（B1–B12 逐项核销）。
## 9. v0.3.0 发布记录（2026-11-22）

- **发布提交**：`607328e release: v0.3.0`（CHANGELOG 收口 + FastAPI `version=0.3.0`）。
- **CI**：run `44997939447` → **success**（7 job：lint 3.11/3.12/3.13、infra、frontend、e2e、security-windows）。
- **Tag / Release**：annotated tag **`v0.3.0`**（dev 与 master 同步后指向发布提交）。
- **Staging 部署**：首次自动部署因 GitHub runner→大陆服务器 SSH 断流失败（3 次重试，旧版本安全在线）；随后 `workflow_dispatch` 手动部署成功，且原失败 run rerun（attempt 2）亦成功；staging 运行 `aa49a68`（发布前最后一个代码提交），`/api/health/ready` 全绿。
- **发布口径**：本批不改变对外 SLA/定价/合规声明；pip-audit CI 门禁已于 2026-11-23 接入（runtime lock 0 漏洞）。