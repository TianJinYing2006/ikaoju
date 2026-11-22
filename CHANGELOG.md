# Changelog

本项目采用 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/) 风格，版本号遵循语义化版本。
对外数字（测试数、提交数等）以 [`docs/figures.md`](docs/figures.md) 为唯一来源，本文件不重复维护易漂移的数字。

## [Unreleased]

### Changed

- **后端按域拆分第五刀（MFA 服务层）**：MFA 的 TOTP 校验/恢复码兜底/step-up 会话轮换逻辑从 `main.py` 收敛到 `web/backend/mfa.py`（80 行，`store`/`store_call`/`set_session_cookies` 显式注入）；`main.py` 保留 5 个 MFA 路由的薄包装，3449 行；行为与需求 42 防重放/失败关闭语义不变，MFA/鉴权/会话测试全绿。下一刀将 MFA 路由与请求模型迁至 `api/mfa.py`。
- **后端按域拆分第四刀（auth 凭据域）**：`POST /api/auth/register`、`POST /api/auth/login`、`POST /api/auth/logout` 与 `RegisterRequest`/`LoginRequest` 从 `main.py` 抽到 `web/backend/api/auth_credentials.py`（177 行），`main.py` 3580→3482 行（-98）；注册开关/限流器/会话 Cookie/审计/CSRF/设备指纹均以注入方式保持原实现，既有鉴权、限流、MFA、会话治理测试全绿。
- **后端按域拆分第三刀（auth 会话域）**：`GET /api/auth/session`、`GET/DELETE /api/auth/sessions` 从 `main.py` 抽到 `web/backend/api/auth_sessions.py`（101 行），`main.py` 3633→3580 行（-53）；会话判定 / CSRF / 重认证限流 / 审计 / Cookie 清理仍由 main 注入，既有会话治理与鉴权测试全绿。
- **后端按域拆分第二刀（ops 域）**：`/api/options`、`/api/metrics`、`/api/ops/alerts` 从 `main.py` 抽到 `web/backend/api/ops.py`（178 行），`main.py` 3753→3633 行（-120）；依赖注入同 health 域，现有选项/指标/告警测试全绿。
- **后端按域拆分第一刀（health 域）**：`/api/health`、`/api/health/live`、`/api/health/ready` 从 `main.py` 抽到 `web/backend/api/health.py`，依赖由组合根以 getter 注入；`main.py` 3861→3753 行（-108），新增 `api/health.py` 159 行。路由/响应/安全披露语义不变，既有健康与运维测试全绿。

### Fixed

- **知识库上传在隔离区卷不可写时不再裸 500**：镜像内预创建并授权 `/var/lib/deepresearch/rag-quarantine`（named volume 首次初始化继承属主，根治新环境复发）；`stream_to_temp` 的 `OSError` 转为结构化 503 `rag_ingest_failed`，客户端只回固定文案、异常原文经 `safe_detail` 只进服务端日志（#324 口径）。取代旧 PR #121。

## [0.3.0] - 2026-11-22

> 2026-11「可审计性与架构收敛」批次：RunContext / run 级 token 账本 / RAG 作用域与运行档位显式注入 /
> 独立裁判与确定性回放脚手架 / CI 门禁（coverage、mypy、Windows 安全 job）与文档口径收口。

### Added

- **`research_engine/runtime/`（ADR-0019）**：`RunContext`（run_id / trace / 取消谓词）收敛一次 run 的运行期句柄；`with_token_writeback` 节点装饰器保证任意节点 delta 都带 `token_used`，新增节点漏写不可能。
- **`tools/gen_figures.py`**：figures 结构事实的机器生成与两层校验（live→JSON 新鲜度、JSON→figures.md 一致性），CI 增加 `--check` step；`tests/test_gen_figures.py`（22 例）。
- **`docs/plan/2026-11-execution.md`**：2026-11 重构批次的完成/未完成/原因与下一步。

### Changed

- **README 重写**：从 531 行收敛为约 190 行；并列披露评测的四个不同口径，明确「是什么 / 不是什么 / 已知边界」；「单元测试 / E2E」数字保持与 figures 机器一致。
- **仓库卫生**：`icon-lab/`、`ppt-report/` 移出 git 跟踪（本地文件保留），`.tmp/` 忽略；figures 代码规模/ADR/pytest 计数按 2026-11-22 实测回填。
- **eval Phase 1 改为 per-run 图实例**：不再共享同一 graph/Agent 状态（并发跑批不再互相污染验证统计与降级记录）。

### Fixed

- **HITL token 计账**：`_clarify` / `_revise_plan` 写回 `token_used`，预算硬闸对 HITL 路径可见（此前静默低估）。
- **压缩失败静默降级**：`ContextManager.compress` 失败现在记录 `DegradationSink` 并经 `_write` 进 `state.degradation_log`，不再把「压缩被绕过」显示为 success。
- **validator fail-open**：无事实句时 `citation_coverage` 返回 `None` 而非 `1.0`。
- **重试绕过输入安全闸**：`retry_run` 复用 `_check_input_safety`（空主题 / 内容审核 / 注入预检）。
- **审计静默丢失**：`_audit` 写库失败改为 ERROR 日志（不含 detail 原文）。
- **Passkey 假 fail-closed**：staging/production 未显式配置 `DR_WEBAUTHN_*` 时 `enabled()` 为 False（原 localhost 默认值导致恒真、选项谎报可用）。

### Security

- Passkey 生产环境 fail-closed（见 Fixed）；审计旁路失败可观测。

### Changed（2026-10 批次，历史条目）

- 注册改为**开放注册**（邮箱 + 密码 + 同意条款），不再需要邀请码；仍受注册/登录限流、单用户每日配额与月度预算约束。
- **quick 档模型改为 `qwen-flash`（Issue #310）**：百炼 `qwen-turbo` 持续 429 `limit_requests` 且 quick 档把四个核心角色钉在 turbo ⇒ quick 不可用；`PROFILE_VERSION` v1→v2，补 qwen-flash 成本单价；standard/deep 不变。

### Removed

- 邀请制/邀请码：后端邀请码校验与 `/api/options.invite_only`、管理 CLI `create-invite` / `list-invites` / `revoke-invite`、前端邀请码字段与 `?invite=` 邀请弹窗、相关 E2E 全部删除。

### Fixed

- **模型限流错误归因与失败进度显示（Issue #308）**：provider 429/5xx/网络异常不再被记为 `internal`，统一为 `llm_error`（非工具层；错误卡可重试、归因 `llm`、文案可行动）；失败/取消/超时的运行进度不再显示「当前阶段 完成 100%」，改为「运行失败 / 已停止 / 已到时限停止」；补齐 `llm_error` / `internal` / `token_limit` / `recursion_limit` 的错误规格。
- **LLM 有界重试（Issue #310）**：任务时限存在时 SDK 重试被关闭（`max_retries=0`）；新增客户端层有界重试，仅对 provider 429/5xx/网络错误生效（默认 2 次、指数退避+抖动、受剩余时限约束，非 provider 错误/流式不重试），避免一次瞬时限流打穿整轮 run。
- 审计整改批次的正确性/健壮性修复（PR 索引见上节 Security 条目）：错误归因与异常原文脱敏、用量原子记账、幂等竞态复核指纹、匿名幂等唯一索引（迁移 0030）、清理任务接线、snapshots 保留期、运维脚本 `backup-verify` 不再假绿 / `migrate` 同事务+hash（迁移 0031）、引擎 D-07/D-08/D-09/D-17 等死代码与结算缺口（#361/#365/#370/#375/#369）。

### Security

- 开放注册后的成本/滥用风险登记：后续评估邮箱验证、CAPTCHA 或邀请式灰度回退开关。
- **2026-11 全仓审计整改批次（需求 42 / Issue #344，31 个 PR #345–#375）**：修复 3 High + 16 Medium + 约 30 Low —— 账号注销链路完整性（撤销活跃分享 + consents/ingestion/quota/export 擦除；#374）；备份/部署链路明文密钥与明文 dump 不再进入 COS（快照排除 `.env*`、`trap` 清理明文、允许清单只同步加密产物；#368，**合并后需按 runbook 盘点存量并轮换可能外泄的密钥**）；代码执行沙箱真实事件名/前缀拒绝 + 危险 C 扩展与 subinterpreter 拦截 + 固定沙箱根 + 最小环境 + 有界输出（#359）；TOTP 绑定要求当前密码（#348）、TOTP 防重放（迁移 0028；#350/#354）、重认证限流与失败审计/登出 CSRF/会话轮换（#363）；客户端错误不再回显异常原文（#347/#352）；上传孤儿清理 + OOXML 绝对展开上限 + 解析墙钟（#357）；分享活跃唯一索引（迁移 0029；#358）；SSRF 加固（CGNAT 判定 + MCP 调用期校验/禁重定向；#372）；web 快照净化与系统提示泄漏标记单一来源（#371/#373）；租约 fence 覆盖 ingestion/deletion/checkpoint（#355）；迁移 0018–0021 补结构断言 + 迁移↔checks 一致性守卫（#349）；前端登出状态清理（#351）、可访问性与密码输入（#353）、跨标签页登出与 reset token 清 hash（#360）；CSP↔Sentry 解耦与静态缓存策略（#346）。索引见 `docs/requirements/42-audit-remediation-2026-11.md`。

## [0.2.0] - 2026-10-26

自 `v0.1.0`（2026-09-07，W1–W6 里程碑）以来的研究引擎、L3 多用户与产品化底座收口版本。

### Added

- **L3 多用户与账户体系（需求 20）**：邀请制注册、`__Host-` Cookie、NIST 口令策略、空闲超时、会话/设备管理、新设备登录提醒、TOTP MFA + 恢复码 + 登录 step-up、Passkey（WebAuthn）注册/登录/管理、邮件自助找回密码、管理 CLI。
- **研究质量与可恢复性（需求 34/35）**：网页快照落盘、逐字引文 + offset、`find_in_source`、证据链分级处置与红线锁定、交付自检、evidence manifest、来源身份分组、PG checkpoint 跨进程恢复、报告流式（REPORT_DELTA/RESET）、档位耗时标定。
- **人机协作与工具面（需求 36/37）**：澄清/计划审批 HITL 回路、报告后追问、MCP 消费端全链路、对外 MCP Server（四工具 + Bearer 鉴权）。
- **导出与分享（需求 26/38）**：统一 `ExportDocument`、md/json/docx 同步导出、PDF 异步导出作业、历史报告导出、只读分享链接（7 天有效 / 可撤销）。
- **知识库（需求 23）**：三层数据/版本化重建、RRF 融合、管理 API 与评测脚手架。
- **产品体验**：品牌更名「爱考据 iKaoju」、研究档案主题工作台与报告版式、证据侧栏、登录/落地页、任务列表搜索/重命名/置顶/归档/重试、移动端适配、ICP 备案展示。
- **可观测与运营**：OTel 原生埋点（traces/metrics）、Prometheus 指标、告警外送（webhook）、错误追踪/反馈/合规页、容量压测与容量模型、申诉/复核状态机、审核 provider 抽象。
- **工程与治理**：CI 6 项检查（lint 矩阵 ×3 + infra + frontend + e2e）、公开仓脱敏守卫、ADR 体系（17 篇）、`docs/figures.md` 单一数字源、CONTRIBUTING v2.2。

### Security

- 公开仓真实 IP 脱敏 + `tools/check_public_ip.py` 守卫；部署主机/密钥迁移 GitHub Secrets，仓库不再保存真实 host。
- 应用层全局安全响应头：CSP、HSTS（staging/production）、`X-Content-Type-Options`、`X-Frame-Options`、`Referrer-Policy`、`Permissions-Policy`。
- MFA step-up 5 次/分/用户、MFA 管理 10 次/分、Passkey 管理 10 次/分（Redis 滑动窗口，进程内兜底）；命中 429 + 审计。
- Passkey 注册要求当前密码重确认（防会话劫持后持久化绑定）。
- 反代信任：compose 默认可信 CIDR + Caddy 覆盖 `X-Forwarded-For`；应用按真实客户端 IP 限流/审计。
- 备份/恢复凭据不再进入 `pg_dump`/`psql`/`pg_restore` argv；`backup-cron.sh` 改用 0600 临时 env-file；生产强制 `BACKUP_REQUIRE_ENCRYPTION=1`。
- 公网 `GET /metrics` 统一由 Caddy 返回 403，指标仅供本机/内网抓取。

### Changed

- **治理**：CONTRIBUTING v2.2 明确 L3-A/B 边界、5 道产品化门禁、PR ≤300 行 / ≤6 文件、发布批次（2~4 周）、三档门禁、公开仓脱敏与 AI 规则；ADR-0017 边界重划、ADR-0009 转正、ADR 索引修正。
- **CI**：PR 校验覆盖所有 base；apt/pango/CJK 字体/Playwright 浏览器缓存；关键步骤超时；e2e 从 10~18 分钟回到 ~5 分钟。
- **部署**：staging 自动部署（CI 绿后）、腾讯云镜像源、部署分段耗时/超时、Caddy SSE 反代、Caddy 配置变更时重建容器再 reload。
- **测试**：`tests/conftest.py` 在零依赖 lane 下忽略 `.env` 来源的 `DR_TEST_*`，本地与 CI 同口径；集成 lane 需 `DR_TEST_INTEGRATION=1` 显式 opt-in。

### Fixed

- 修复本机 `.env` 配置不可达 PG/Redis 时 80 个假失败（掩盖真回归）。
- 修复 CI apt/字体/Playwright 偶发 10~18 分钟等待。
- 修复部署 `caddy reload` 静默不生效（单文件 bind mount 旧 inode + 失败只 WARN）。
- 修复安全响应头缺失、MFA/Passkey 接口无限流、Passkey 注册无重认证、备份 DSN argv 泄漏、公网 `/metrics` 暴露。
- 修复 staging 构建 apt/pip 镜像超时、部署脚本无阶段超时。

### Known limitations

- 当前处于 **L3-A/B 邀请制内测（≤10 用户）**，5 道产品化门禁未全部通过（ADR-0017）。
- 服务器默认口令轮换、公网 IP 更换/安全组（P0-6）仍需运维窗口。
- **应用版本回滚演练未执行**：SSH forced command 只允许默认 `deploy.sh`，无法远程传 `--rollback`；需服务器侧受控入口（Issue #299，runbook §9 如实登记）。
- 前端 Sentry 若启用，需通过 `DR_CSP_CONNECT_SRC` 追加其 origin，否则上报会被 CSP 拦截。
- 行级安全（RLS，P0-7）尚未评估/实施。

### Upgrade notes

- 发布前先做备份并验证可读（`tools/backup.sh` + `tools/backup-verify.sh`）；迁移 0001–0027 只前向，数据回退依赖备份恢复。
- 应用回滚使用镜像 tag `rollback-<sha>`（`deploy.sh --rollback`）；数据库不回滚。
- 运行环境：Python 3.11–3.13、Node 22、PostgreSQL 16、Redis、Qdrant、MinIO/COS。

## [0.1.0] - 2026-09-07

W1–W6 里程碑版本（初始研究引擎、Web 工作台与基础部署）。

[0.3.0]: https://github.com/TianJinYing2006/DeepResearch/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/TianJinYing2006/DeepResearch/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/TianJinYing2006/DeepResearch/releases/tag/v0.1.0
