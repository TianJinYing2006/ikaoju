# 当前状态快照（单页）

> **更新**：2026-11-22 ｜ **分支**：`plan/2026-11-refactor`（重构批次，未合入 dev）
> **定位**：本页只回答「现在能做什么 / 正在做什么 / 不可宣称什么」。
> 历史长日志见 [`docs/project-status.md`](../project-status.md)（已归档为按时间追加的长文）；
> 批次级完成情况见 [`docs/plan/2026-11-execution.md`](../plan/2026-11-execution.md)；
> 对外数字见 [`docs/figures.md`](../figures.md)。

## 1. 稳定可用（v0.2.0 主线）

| 能力 | 证据/入口 |
| --- | --- |
| 研究图：plan → research ⇄ critic → write → validate →[repair]→ render | `research_engine/graph.py`；ADR-0001/0019 |
| 多跳检索：Web（博查/Tavily）+ 私有 RAG + arXiv；预算与硬闸 | `research_engine/agents/researcher.py`、`research_engine/critic.py` |
| 证据链：内容寻址快照 + 逐字引文 + 字符偏移 + 分级处置 | `research_engine/evidence*.py`、`quote_locate.py` |
| 故障可归因 + 可复现：failure_reason / config 快照 / prompt hash / scorer version | `research_engine/failure_reasons.py`、`eval/provenance.py` |
| Web 应用：FastAPI + SSE（AG-UI 语义）+ React 前端；取消/刷新恢复/导出 md/json/docx/PDF | `web/backend/`、`web/frontend/` |
| 账号/配额/限流/多租户 RAG/内容安全/只读分享/用量账本 | `web/backend/{auth,store,rag_pipeline,moderation}.py` |
| 部署与运维：compose（PG+Redis+MinIO+worker）、迁移只前向、备份→恢复演练 | `docker-compose.staging.yml`、`tools/backup.sh`、CI `infra` |
| 质量基线：1531 收集 / 1451 通过 / 81 跳过；6 守卫；coverage 75% | 见 `docs/figures.md` §3 |

## 2. 进行中（本次重构批次，已完成项）

- Phase 0 止血四件套：eval 并发隔离、HITL token 写回、压缩降级可归因、coverage fail-open。
- 引擎：`RunContext` + 节点 token 写回装饰器 + ADR-0019；评测 Phase 1 per-run 图实例。
- 评测：figures 机器校验（`tools/gen_figures.py`，CI `--check`）；独立裁判脚手架（`--judge-model/--judge-family`，provenance 动态判定）。
- 确定性回放：`SEARCH_PROVIDER=replay` + `--replay-dir`（固定检索输入，arXiv 同时关闭）。
- 后端：`web/backend/api/meta.py` 薄路由层第一刀；重试输入安全闸、审计失败日志、Passkey 生产 fail-closed。
- CI：coverage 门禁（3.13 档 ≥73%）、Windows 专属沙箱安全 job。
- 仓库卫生：`icon-lab/`、`ppt-report/` 移出跟踪（本地保留）；README 重写为诚实口径。

## 3. 未完成 / 不宣称

| 项 | 状态 |
| --- | --- |
| 评测 v2 实跑（固定证据池 + 跨模型族裁判 + 人工抽检 + RAG 评测集） | 脚手架已就绪，**需 API key/预算**；现有 20 题自建集不支撑质量结论 |
| 引用准确率 | 多口径（43.3~85.4%），**不可直接比较**；见 README「评测」与 `docs/eval-w9-baseline.md` |
| 研究质量 | coverage ≈40%、uncited_fact_ratio ≈69%、信息不足标记 ≈74~79% —— 未达可宣称水平 |
| 引擎彻底去隐式依赖（类级 LLM 计数 / contextvar） | 部分：RAG 作用域已显式注入；**类级 LLM 计数器已删除**（引擎/评测/追问走 run 级 TokenLedger + run_id 注册表；`tokens_diff` 废弃）；剩余：runtime_profile/usage contextvar |
| 后端完整拆分（app factory + routers + services + repositories） | 仅完成 `api/meta.py` 样板 |
| 剩余 CI 门禁（pip-audit / mypy 增量）、npm devDeps 审计 | 未做 |
| 生产就绪 | 审核默认本地空词表、无邮箱验证/CAPTCHA、沙箱非恶意代码隔离 —— **不宣称生产级** |

## 4. 下一步优先级

1. 评测 v2 实跑并冻结新基线（质量说服力的唯一来源）；
2. 引擎隐式依赖收敛 + 后端按域拆分（每 PR 带契约测试）；
3. pip-audit / mypy 增量门禁；
4. 截图/90s demo 与 v0.3.0 发布。
