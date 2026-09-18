# FastAPI
Repository of fastapi backend development(with agent)


## 企业知识库智能问答助手（独立开发 · 后端负责人）

**技术栈**：Python 3.12、FastAPI、SQLAlchemy 2.0、MySQL 8、Pydantic v2、LangChain 1.x + LangGraph、Chroma 向量库、JWT + bcrypt、Loguru、PyMySQL

**项目简介**：面向企业内部员工的 RAG 知识库问答系统。支持多格式文档上传解析、向量化语义检索、Agent 自主决策检索、SSE 流式打字机回答、答案引用溯源、多用户数据隔离与操作审计。后端分层架构，6 张业务表、31 个 REST 接口、约 9,400 行代码，从 0 到 1 独立完成设计、编码、测试与文档。

**核心工作**：

1. **分层架构与统一工程规范**：按 `API → Service → CRUD → Model` 四层组织代码，Pydantic v2 承担入参校验与出参脱敏；抽象泛型 `CRUDBase`（`Generic[Model, CreateSchema, UpdateSchema]`）统一复用增删改查/软删除/分页，避免每张表重复样板代码；全站统一响应体 `{code, message, data, request_id, timestamp}` + 业务码枚举，四类全局异常处理器（业务/参数校验/数据库/兜底）把技术异常转成用户可读提示，不向前端泄露堆栈、SQL 与密钥。
2. **认证授权与多租户数据隔离**：bcrypt（rounds=12，自带随机盐）哈希密码 + JWT 双令牌（access 1 天 / refresh 7 天），payload 内 `type` 字段校验防止 refresh_token 越权访问业务接口；以 FastAPI 依赖注入实现 `get_owned_conversation / get_owned_document` 归属校验，把 `WHERE user_id = ?` 下沉到 SQL 层，越权统一返回 **404 而非 403**，避免通过状态码枚举他人会话 ID；消息表冗余 `user_id` 作为隔离二次兜底。
3. **RAG 全链路实现**：上传 → 后缀白名单/大小校验 → SHA256 内容哈希查重（同文件秒传，不重复消耗 Embedding 费用）→ UUID 前缀存储名（防同名覆盖与目录穿越）→ 多格式解析（PDF/Word/TXT/MD/CSV，含 UTF-8/GBK/GB18030 编码回退、Word 表格内容抽取、PDF 页码保留）→ `RecursiveCharacterTextSplitter` 按「段落→换行→中文句号→分号→逗号」自然边界切片（600 字符 / 100 重叠，均可配置）→ Embedding 分批（16 条/批）写入 Chroma（cosine 距离）→ 切片原文回写 MySQL 支撑切片预览与免解析重建索引；状态机 `pending→parsing→embedding→completed/failed` 全流程可观测，失败自动清理写了一半的向量并回写失败原因；删除按「先删向量 → 再删库记录 → 再删磁盘文件」顺序执行，杜绝「文档已删但问答仍能引用」的脏数据。
4. **向量检索的权限过滤（安全关键点）**：切片元数据中固化 `owner_id / is_public`，检索时用 Chroma `where: {$or: [{is_public: true}, {owner_id: uid}]}` 做可见性过滤，并与文档列表接口**共用同一套可见性规则**，从设计上保证「列表里看得到的文档 == 问答能检索到的文档」；距离统一转换为相似度（`1 - distance`）便于前端展示；额外提供不经过大模型的「检索测试」接口，用于验证入库结果与调优 `top_k / chunk_size`。
5. **LangChain Agent 与流式问答**：基于 LangChain 1.x `create_agent`（底层 LangGraph `StateGraph`）实现 ReAct 智能体，由模型自主决定「是否需要检索 / 是否换关键词重查」，`recursion_limit=12` 防止工具调用死循环；检索工具用**闭包把 `user_id` 焊死在工具内部**，模型无法通过工具参数篡改身份（防提示词注入式越权）；用 `astream_events(v2)` 消费 `on_chat_model_stream / on_tool_end / on_chain_end` 事件，实现 token 级 SSE 流式输出 + 引用来源增量推送，并采集 token 用量、耗时、模型名落库，形成大模型应用的用量与成本数据。
6. **流式场景下的资源生命周期治理**：识别并解决 `StreamingResponse` 生成器执行时请求作用域 Session 已关闭的典型坑——所有 DB 写操作前置到「流开始前」，收尾落库改用独立 `SessionLocal`，副作用操作统一 try/except 包裹（保存失败不影响用户已看到的回答）；SSE 帧经 `json.dumps` 转义防止裸换行破坏协议，配合 `X-Accel-Buffering: no` 与心跳注释帧解决 Nginx 缓冲导致的「流式变一次性返回」问题。
7. **可观测性与审计**：`request_id` 通过 `contextvars` 贯穿日志、响应头 `X-Request-ID` 与响应体，实现单请求全链路串联；访问日志中间件记录方法/路径/状态码/IP/耗时，≥500ms 记慢请求告警并回写 `X-Process-Time-Ms`；启动配置摘要对 API Key、密码、JWT 密钥统一打码，满足日志脱敏要求；登录、注册、上传、删除、问答等关键操作全部写入审计表。
8. **质量保障体系**：自研零第三方依赖的测试入口（未装依赖时可用内置桩模块 `--offline` 运行），**67 个用例**基于 SQLite 内存库执行并显式开启外键约束（对齐 MySQL 级联删除行为），覆盖密码哈希、认证、会话生命周期、历史上下文、**跨用户越权防护**与文档可见性；另有架构自检覆盖 6 张 ORM 表的 MySQL 方言 DDL 编译、Pydantic 负向校验、CRUD 方法齐备性与 31 条路由的鉴权声明；配套 `preflight_check.py` 启动前自检（依赖/配置/MySQL 连通性/大模型与 Embedding 可达性）与 `reindex.py` 向量索引重建脚本（换 Embedding 模型后一键重建）。

**项目成果**：

- 独立交付完整后端系统：6 张业务表、31 个 REST 接口、约 9,400 行代码，含接口文档、架构说明、配置指南与部署文档；
- 安全设计闭环：JWT 双令牌 + SQL 层数据隔离 + 越权防护 + 全量审计 + 日志脱敏；
- 模型可插拔：切换 DeepSeek / 通义 / Kimi / 智谱 / 本地 vLLM 只需改 3 个配置项，业务代码零改动；
- 交付质量：67 个用例 + 3 组启动自检全通过，测试不依赖 MySQL、不留垃圾文件。
