# ADR-0019：一次 run 的运行期上下文与节点 token 写回

## 基本信息

- **编号**：0019
- **标题**：用 `RunContext` 收敛 run 级身份，并把节点 token 写回从「逐节点自觉」改为「包装器保证」
- **日期**：2026-11-22
- **状态**：已采纳
- **涉及模块**：`research_engine/graph.py`、`research_engine/runtime/`、`research_engine/eval/run.py`、`research_engine/context/manager.py`、`web/backend/main.py`

## 背景

引擎把一次 run 的运行期信息分散在三个地方：

1. `DeepResearchGraph` 的**实例属性**（`trace_id`/`last_exception`/`last_stop_reason`/`tokens_diff`）；
2. `run()` / `iter_run()` 的**局部变量**（`thread_id`、`should_cancel`、`token_base`）；
3. **Agent 实例上的可变状态**（`Validator.last_validation_stats`、各 Agent 的 degradation 缓冲）。

同时，token 用量虽然由 `budget` 原子写进 `state.token_used`，但每个节点都要**手写**
`"token_used": state.token_used` 才会被 LangGraph 通道合并。这是「靠自觉维护的不变量」。

## 问题 / 动机

2026-11 全仓审计确认了三类同源缺陷：

- **eval 并发串台**：`eval/run.py` 建一份 `graph` 交给 3 线程共享，Agent 实例状态跨题互相污染，
  被污染的是评测证据本身；
- **HITL 预算失明**：`_clarify` / `_revise_plan` 调用 LLM 后漏写 `token_used`，
  这两个节点的消耗对预算硬闸不可见；
- **新增节点必漏**：没有结构性保证，任何新节点忘记写回都会静默复发。

## 方案

1. 新增 `research_engine/runtime/`：
   - `RunContext(run_id, trace_id, cancel, started_at)`：一次 run 的显式句柄，
     **只属于一次 run，禁止跨 run 共享**；提供 `cancelled()` / `elapsed_s()` / `as_dict()`。
   - `new_run_context()`：唯一构造入口。
   - `with_token_writeback`：节点装饰器，节点 delta 缺 `token_used` 时自动补当前
     `state.token_used`，并打 `__token_writeback__` 标记。
2. `DeepResearchGraph` 的 12 个节点全部套装饰器；删除各节点手写的 `token_used` 行。
3. `run()` / `iter_run()` 统一经 `new_run_context()` 建上下文；取消谓词归一后传给
   `_iter_steps`（不传谓词时仍传 `None`，保持「不取消」语义无副作用）。
4. `eval/run.py` 改为 **per-run 图实例**：`_run_one(graph=None)` 在当前线程内
   `create_graph()`；线程池不再共享同一 graph。
5. 压缩失败（`ContextManager.compress`）改为走 `DegradationSink` 并在 `_write` 节点
   drain 进 `state.degradation_log`，`run_status` 不再把「压缩被绕过」显示为 success。

## 理由与取舍

- **不做**「完全重写为依赖注入容器」：`config` 模块级单例、`get_router()` 单例被 300+ 处引用，
  一次性替换的回归面远大于收益。本次只收敛「run 级、天然可重复」的部分。
- **不做** contextvar → 显式参数的全量改造：先消灭「实例状态被多 run 共享」这个真实缺陷，
  其余留待各模块边界拆分时逐步做。
- 代价：`with_token_writeback` 依赖节点签名 `(self, state)`；新增节点若签名不同，
  装饰器会读不到 state（结构测试锁定了生产节点集合）。
- 代价：`_iter_steps` 的参数语义仍是「取消谓词」，而不是完整的 RunContext；
  RunContext 目前只用于 `run()/iter_run()` 边界。后续可把 `token_base`/trace 一并迁入。

## 影响

- **评测数据可信度**：并发跑批不再互相污染验证统计与降级记录（`tests/test_eval_per_run_isolation.py`）。
- **预算硬闸**：HITL 路径 token 对硬闸可见（`tests/test_hitl_token_accounting.py`）。
- **可归因**：压缩降级不再静默（`tests/test_context_compression.py` 新增两类用例）。
- **结构性防回归**：`tests/test_run_context.py` 断言全部 12 个生产节点都带写回标记；
  新增节点漏套装饰器会红。
- 兼容性：`run()` / `iter_run()` / `create_graph()` 外部签名不变；`graph.trace_id` 等属性保留。

## 变更记录

| 日期 | 变更说明 |
|------|---------|
| 2026-11-22 | 初始记录；同批修复 eval 并发、HITL 计账、压缩静默降级 |
