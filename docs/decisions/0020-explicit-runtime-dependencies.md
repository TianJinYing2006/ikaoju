# ADR-0020：运行期依赖显式化（RAG 作用域注入 + run 级 TokenLedger）

## 基本信息

- **编号**：0020
- **标题**：把「运行期隐式依赖」收敛为显式注入：RAG 检索作用域与 token 账本
- **日期**：2026-11-22
- **状态**：已采纳（实施中）
- **涉及模块**：`research_engine/graph.py`、`research_engine/agents/researcher.py`、`research_engine/rag/scope.py`、`research_engine/runtime/ledger.py`、`research_engine/llm/client.py`、`web/backend/runner.py`、`web/backend/worker.py`

## 背景

ADR-0019 收敛了「一次 run 的句柄」（`RunContext`）与节点 token 写回，但引擎仍有两类**隐式依赖**：

1. **RAG 检索作用域**走 `contextvars`：执行器必须先 `set_scope()`、再 `create_graph()`，
   `Researcher` 在构造时捕获——顺序错了就静默退化为匿名作用域（staging 上表现为 RAG 恒零命中）。
2. **token 统计**走 `LLMClient` 的**类级**计数器：跨 run、跨线程全局，并发 run 互相污染，
   且引擎运行期依赖一个全局可变对象判断「本次 run 用了多少」。

## 方案

### 1. RAG 作用域显式注入

- `create_graph(..., rag_scope=RagScope(...))` → `DeepResearchGraph` → `Researcher(rag_scope=...)`；
- `create_graph.accepts_rag_scope = True` 能力标记；runner/worker 探测到标记就显式注入，
  否则回退旧的 `set_scope()` 路径（测试替身/DemoGraph 不受影响）；
- `contextvars` 保留为库函数/测试兜底，主链路不再依赖「set_scope 先于建图」。

### 2. run 级 token 账本

- 新增 `runtime/ledger.py`：`TokenLedger`（按 model / role / input-output 双轨，线程安全）；
- `RunContext` 持有账本；`graph.run/iter_run` 把 `state.run_id` 写入状态，并
  `register_ledger(run_id, ledger)` 登记进程级注册表（run 结束注销）；
- `LLMClient` 记账时按 `state.run_id` 找到本 run 账本写入；**引擎运行期不再读类级计数器**；
- `graph.last_ledger` 暴露快照；旧 `tokens_diff` 字段保留但恒 `None`（兼容旧调用方），
  CLI 改为打印 run 账本（总量 + 模型桶）。

### 3. 为什么账本不放 `ResearchState` 的私有属性

第一版把账本挂成 Pydantic `PrivateAttr`，HITL interrupt 的 checkpoint 用 msgpack
序列化 state 时直接报 `Type is not msgpack serializable: TokenLedger`。
结论：**可序列化状态只放 `run_id` 字符串**，账本留在进程注册表——跨进程恢复的 run
从恢复点重新累计（成本权威仍是 `usage_ledger` / `state.token_used`）。

## 理由与取舍

- **不做**全量 DI 容器改造：`config`/`get_router()` 被 300+ 处引用，一次性替换回归面过大；
  只收敛「引擎运行期真正影响正确性」的两处。
- 代价：进程注册表是进程内状态；多进程 Worker 各自独立（符合 per-run 语义）。
- 代价：评测工具的跨 run 汇总与 judge 成本已迁移到 run 级账本（2026-11-22）；类级计数器已删除（登记在
  `docs/plan/2026-11-execution.md`）。

## 影响

- 多租户 RAG 隔离不再依赖调用顺序（`tests/test_rag_scope.py` 新增 3 例）；
- 并发 run 的 token 统计互不污染（`tests/test_run_ledger.py` 7 例）；
- HITL / Arm1 / 审计低危存量测试同步适配并全绿。

## 变更记录

| 日期 | 变更说明 |
|------|---------|
| 2026-11-22 | 初始记录：RAG 作用域显式注入 + run 级 TokenLedger（引擎去类级计数器） |
