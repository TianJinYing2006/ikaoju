"""Web API 薄路由层（2026-11 起逐步从 ``main.py`` 拆出）。

拆分原则（见 docs/plan/2026-11-execution.md §3）：
- 本层只做 HTTP 映射（请求模型 / 状态码 / 序列化），不承载业务与 SQL；
- 不 import 模块级单例；依赖由 ``main.py`` 装配后传入或在 ``api`` 内自包含（如本文件的静态文档路由）；
- 每拆一个域，``main.py`` 改为 ``include_router``，测试全绿后再删旧代码。
"""
