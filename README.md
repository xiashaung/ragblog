# FastAPI 标准工程

一个分层清晰、开箱即用的 FastAPI 项目模板。

## 目录结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py              # 应用入口，组装 FastAPI 实例（lifespan 中初始化 DB；可选挂载 web/dist）
│   ├── config.py            # 基于 pydantic-settings 的配置（读取 .env）
│   ├── db.py                # 异步引擎 + Session 工厂（get_db 依赖）
│   ├── models.py            # SQLAlchemy ORM 模型（用户 / 商品 / RAG / API Key / 角色）
│   ├── rbac.py              # 权限码目录 + 内置角色种子（权限的唯一来源）
│   ├── deps.py              # 鉴权依赖：require_api_key / require_admin_principal / require_permission
│   ├── bootstrap.py         # CLI：python -m app.bootstrap seed（建首个超管）
│   ├── routers/             # 路由层（只处理 HTTP 细节）
│   │   ├── health.py        # /health 健康检查
│   │   ├── users.py         # /api/v1/users CRUD
│   │   ├── items.py         # /api/v1/items CRUD
│   │   ├── ai.py            # /api/v1/ai/*（LLM + RAG）
│   │   ├── ai_keys.py       # /api/v1/ai/keys（API Key 管理，key:read / key:manage）
│   │   └── admin.py         # /admin/*（登录 + 账号 / 角色 / 会话，全部挂权限码）
│   ├── schemas/             # Pydantic 请求/响应模型
│   │   ├── user.py
│   │   ├── item.py
│   │   ├── ai.py
│   │   └── admin.py         # 登录 / Principal / Role / Account / Permission 模型
│   └── services/            # 业务逻辑层（异步 DB 实现）
│       ├── user_service.py
│       ├── item_service.py
│       ├── admin_service.py  # 登录 / 会话 token / 首次引导
│       ├── rbac_service.py   # 权限解析 + 角色/账号 CRUD + 防提权守卫
│       ├── llm.py           # LLM 网关（多 provider 注册表：openai/deepseek/qwen/ollama/mlx/mock）
│       ├── api_keys.py      # API Key + 每小时配额
│       └── rag.py           # RAG：分块 + 向量检索（pgvector / JSON 双路径，多 provider 感知）
├── alembic/                 # 数据库迁移（异步）
│   ├── env.py
│   └── versions/
├── alembic.ini
├── tests/
│   ├── conftest.py          # 异步 TestClient（httpx ASGI transport）+ console/account fixture
│   ├── test_api.py          # 基础 CRUD 测试
│   ├── test_ai.py           # AI / RAG / Key 测试
│   └── test_rbac.py         # RBAC：引导、权限矩阵、提权与锁死守卫
├── web-vben/                # 前端源码（Vue 3 + Vite + Ant Design Vue 4 + Pinia）
│   ├── src/api/             # request.ts（envelope + Bearer/X-API-Key 拦截器）、index.ts
│   ├── src/store/modules/   # auth.ts（token + roles/permissions）、business.ts
│   ├── src/router/          # 路由 + meta.permissions 守卫（不足跳 /403）
│   ├── src/layouts/         # MainLayout.vue：菜单按权限过滤
│   └── src/views/           # 页面：auth / _core / ai / rag / keys / admin（账号、角色）
├── web/dist/                # 前端构建产物（被 FastAPI 静态托管，由 web-vben 输出）
├── pyproject.toml
├── .env.example
└── README.md
```

## 快速开始

```bash
# 创建虚拟环境（推荐 uv 或 python -m venv）
python -m venv .venv
source .venv/bin/activate

# 安装依赖
pip install -e ".[dev]"

# 复制配置模板
cp .env.example .env

# 启动开发服务器（http://127.0.0.1:8000）
uvicorn app.main:app --reload
```

交互式文档: http://127.0.0.1:8000/docs

## 运行测试

```bash
pytest
```

## 常用端点

| 方法   | 路径                  | 说明         |
| ------ | --------------------- | ------------ |
| GET    | /health               | 健康检查     |
| GET    | /health/live          | 存活探针     |
| GET    | /health/ready         | 就绪探针     |
| POST   | /api/v1/users         | 创建用户     |
| GET    | /api/v1/users         | 用户列表     |
| GET    | /api/v1/users/{id}    | 查询用户     |
| PATCH  | /api/v1/users/{id}    | 更新用户     |
| DELETE | /api/v1/users/{id}    | 删除用户     |
| POST   | /api/v1/items         | 创建条目     |
| GET    | /api/v1/items         | 条目列表     |
| GET    | /api/v1/items/{id}    | 查询条目     |
| DELETE | /api/v1/items/{id}    | 删除条目     |

## 管理后台与 RBAC 权限

后台管理面（账号 / 角色 / 会话 / API Key）走一套 RBAC：**权限码由代码定义，角色由数据库承载**。
权限码是唯一来源（`app/rbac.py` 的 `Permission` 枚举 + `PERMISSION_CATALOG`，带中文名与分组），
角色则是运营数据，可在「角色权限」页自由增删改。

### 认证方式

```text
POST /admin/login  {username, password}
  → {token, admin, roles, permissions}      # 同时写 http-only cookie: admin_token
后续管理接口带：Authorization: Bearer <token>
```

登录失效返回 `401`，权限不足返回 `403`（message 为中文，形如
`权限不足，需要：管理角色（role:manage）`）。

### 权限码与内置角色

| 权限码          | 含义                     |
| --------------- | ------------------------ |
| `account:read`  | 查看后台账号与角色       |
| `account:manage`| 新建 / 停用 / 删除账号、重置密码、分配角色 |
| `role:read`     | 查看角色与权限目录       |
| `role:manage`   | 新建 / 修改 / 删除角色（提权敏感，仅超管可授予） |
| `session:read`  | 查看在线会话             |
| `session:revoke`| 强制下线某个会话         |
| `key:read`      | 查看 API Key 列表与用量  |
| `key:manage`    | 生成 / 吊销 API Key      |

| 内置角色        | 权限                                             |
| --------------- | ------------------------------------------------ |
| `superadmin`    | `*`（全部权限，恒不可改，只有这一个角色能持有 `*`） |
| `admin`         | 除 `role:manage` 外的全部                         |
| `operator`      | `session:read/revoke` + `key:read/manage`        |
| `viewer`        | 四个 `:read`（只读）                             |

内置角色不可删除、标识不可改；一个账号可挂多个角色，权限取并集。

### 端点与所需权限

| 方法   | 路径                                | 所需权限          |
| ------ | ----------------------------------- | ----------------- |
| POST   | /admin/onboard                      | 公开 + 引导密钥   |
| POST   | /admin/login /logout /refresh       | 登录 / 会话       |
| GET    | /admin/me                           | 登录（任何权限）  |
| GET    | /admin/sessions                     | `session:read`    |
| DELETE | /admin/sessions/{id}                | `session:revoke`  |
| GET    | /admin/permissions、/admin/roles    | `role:read`       |
| POST   | /admin/roles                        | `role:manage`     |
| PATCH  | /admin/roles/{id}                   | `role:manage`     |
| DELETE | /admin/roles/{id}                   | `role:manage`     |
| GET    | /admin/accounts                     | `account:read`    |
| POST   | /admin/accounts                     | `account:manage`  |
| PATCH  | /admin/accounts/{id}                | `account:manage`  |
| PUT    | /admin/accounts/{id}/roles          | `account:manage`  |
| DELETE | /admin/accounts/{id}                | `account:manage`  |
| GET    | /api/v1/ai/keys                     | `key:read`        |
| POST   | /api/v1/ai/keys                     | `key:manage`      |
| DELETE | /api/v1/ai/keys/{id}                | `key:manage`      |

### 防提权 / 防锁死守卫

集中在 `app/services/rbac_service.py`，路由层不管这些：

- 非超级管理员不能创建/修改含 `role:manage` 的角色，也不能把这类角色分配给账号；
- `*` 只能属于内置 `superadmin`（否则可以伪造一个“隐藏的超管”绕过下面条规则）；
- 不能停用、删除、或收回**自己**的超管角色，也不能移除最后一个可用超管的超管角色；
- 停用账号 / 删号 / 重置密码会同时吊销该账号的全部在线会话；
- 有成员的角色不能删除，内置角色不能删除。

### 首次引导

`APP_ADMIN_API_KEY` 现在**只有一个用途**：作为 `X-API-Key` 头证明调用方是运维本人，
抢注库里第一个超管（`POST /admin/onboard`）。它不再是任何管理接口的旁路，
也不再生效于配额豁免。引导完成后建议置空以关闭 HTTP 入口，改用 CLI：

```bash
# 方式一（需 APP_ADMIN_API_KEY）：界面 /admin/onboard 或 curl
# 方式二（推荐，不开放 HTTP 入口）：
python -m app.bootstrap seed --username admin --password 'xxx'
```

> 不要直接往 `admin_users` 插行：没有角色绑定的账号登录后哪个管理接口都进不去。
> 升级时迁移（`f5a6b7c8d9e0`）会把一切现存账号绑到 `superadmin`，语义与升级前的
> 「唯一管理员」等价，不会降权。

> 尚未纳入 RBAC：`/api/v1/users`、`/api/v1/items` 仍为开放示例接口；
> `/api/v1/ai/*`（对话/向量化/知识库）只校验 API Key 是否有效与配额，不区分权限码。

## AI 接口

封装了一套面向 AI 调用的接口，**支持在请求级别切换多家模型平台**
（OpenAI / DeepSeek / 通义千问 / Ollama / MLX / 内置 mock），
每家平台有独立的 `api_key` / `base_url` / 模型名配置。
请求体里传 `provider` 字段即可切换；不传则回退到
`APP_LLM_DEFAULT_PROVIDER`。

### 端点

| 方法   | 路径                            | 说明                                   |
| ------ | ------------------------------- | -------------------------------------- |
| POST   | /api/v1/ai/chat                 | 对话补全，支持流式/结构化 JSON 输出    |
| POST   | /api/v1/ai/embeddings           | 文本向量化（批量，单次 ≤2048 条）      |
| POST   | /api/v1/ai/knowledge/ingest     | RAG：录入文本（自动分块 + 向量化）      |
| POST   | /api/v1/ai/knowledge/ingest-pairs | RAG：录入一批问答对（合为一篇 FAQ）   |
| POST   | /api/v1/ai/knowledge/fetch-url  | RAG：抓取网页正文后入库                |
| POST   | /api/v1/upload                  | RAG：单文件入库（pdf/docx/txt/md/csv/log） |
| POST   | /api/v1/upload/batch            | RAG：批量文件入库（≤20 个，逐文件返回成败） |
| GET    | /api/v1/ai/knowledge            | RAG：文档列表（关键字 / provider / 仅待重建 / 分页） |
| GET    | /api/v1/ai/knowledge/stats      | RAG：文档·分片·字符统计与 provider 明细 |
| GET    | /api/v1/ai/knowledge/{id}       | RAG：文档详情（原文 + 分片）           |
| PATCH  | /api/v1/ai/knowledge/{id}       | RAG：改名 / 换正文（换正文会重新向量化） |
| POST   | /api/v1/ai/knowledge/{id}/reindex | RAG：用当前默认 provider 重建向量     |
| DELETE | /api/v1/ai/knowledge/{id}       | RAG：删除文档                          |
| POST   | /api/v1/ai/knowledge/batch-delete | RAG：批量删除，返回 deleted / failed  |
| POST   | /api/v1/ai/knowledge/search     | RAG：向量检索，返回引用片段            |
| POST   | /api/v1/ai/knowledge/answer     | RAG：检索 + 带引用的回答               |
| POST   | /api/v1/ai/keys                 | 生成调用本服务的 API Key（管理员）      |
| GET    | /api/v1/ai/keys                 | 列出 Key 及本小时用量（管理员）         |
| DELETE | /api/v1/ai/keys/{id}            | 吊销 Key（管理员）                     |
| GET    | /api/v1/ai/keys/me              | 当前 Key 自校验（管理员/普通 Key 均可） |
| GET    | /api/v1/ai/providers            | 各 provider 的默认配置（供前端展示）    |

> 不同 provider 的向量不可比：换 `APP_LLM_DEFAULT_PROVIDER` 后，旧文档会被
> 标记为 `stale`（列表接口 `only_stale=true` 可单独筛出），需调 `reindex` 重建。

> 向量存储：Postgres 下 `knowledge_chunks.vector` 是 pgvector 的
> `vector(<APP_RAG_VECTOR_DIM>)` 定长列，相似度由库内余弦距离 `<=>` 计算并走 HNSW
> 索引；SQLite 下同一列退化为 JSON，改由 Python 算余弦（测试与轻量开发无需扩展）。
> 换 embedding 模型导致维度变化时，改 `APP_RAG_VECTOR_DIM` 并重跑
> `alembic upgrade head`：维度不符的旧向量会先备份进 `knowledge_chunks_vector_backup`
> 再置空（统计接口的 `vectorless_chunks` 会报出来），之后对文档重建索引。

### 可选 Provider

| 名称       | 说明                                       | 需要 Key | 默认 base_url                              | 默认 chat_model                          | 默认 embedding_model               |
| ---------- | ------------------------------------------ | -------- | ------------------------------------------ | ---------------------------------------- | ---------------------------------- |
| `openai`   | OpenAI 官方（及任意 OpenAI 兼容网关）      | 是       | `https://api.openai.com/v1`                | `gpt-4o-mini`                            | `text-embedding-3-small`           |
| `deepseek` | DeepSeek（OpenAI 兼容）                    | 是       | `https://api.deepseek.com/v1`              | `deepseek-chat`                          | `deepseek-embed`                   |
| `qwen`     | 阿里通义 / DashScope（OpenAI 兼容）        | 是       | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus`                    | `text-embedding-v3`                |
| `ollama`   | 本地 Ollama 服务器                         | 否       | `http://localhost:11434/v1`                | `llama3.1`                               | `nomic-embed-text`                 |
| `mlx`      | 本地 MLX-LM 服务器                         | 否       | `http://localhost:8080/v1`                 | `mlx-community/llama-3.1-8b-instruct`    | `mlx-community/bge-small-en-v1.5`  |
| `mock`     | 内置离线后端（回显 + 哈希向量，无网络）    | 否       | —                                          | `mock-chat`                              | `mock-embed`                       |

前 5 家共用同一个 OpenAI 兼容客户端，只有 `api_key` / `base_url` / 模型名不同；
`mock` 是确定性的离线后端，供开发 / 测试使用。

### 鉴权

- 所有 `/ai/*` 接口需 `X-API-Key: <key>` 或 `Authorization: Bearer <key>`，这里的 key 是**库里的 API Key**。
- `APP_ADMIN_API_KEY` 不再能调用 `/ai/*`（也不再生效配额豁免）；API Key 由后台管理接口
  生成：`POST /api/v1/ai/keys`（需控制台 Bearer 会话 + `key:manage`）。
- 每个 Key 有独立小时配额（`quota_per_hour`），超限返回 `429`。

### 示例

```bash
# 先登录控制台，再用 key:manage 权限建一个调用用 Key
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/admin/login -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"xxx"}' | sed -n 's/.*"token":"\([^"]\+\)".*/\1/p')
KEY=$(curl -s -X POST http://127.0.0.1:8000/api/v1/ai/keys -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"name":"dev","quota_per_hour":1000}' \
  | sed -n 's/.*"key":"\([^"]\+\)".*/\1/p')

# 对话（走默认 provider）
curl -X POST http://127.0.0.1:8000/api/v1/ai/chat \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"你好"}]}'

# 指定 provider（千问）
curl -X POST http://127.0.0.1:8000/api/v1/ai/chat \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"provider":"qwen","messages":[{"role":"user","content":"你好"}]}'

# 指定 provider + 覆盖模型
curl -X POST http://127.0.0.1:8000/api/v1/ai/chat \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"provider":"deepseek","model":"deepseek-reasoner","messages":[{"role":"user","content":"1+1?"}]}'

# 本地 Ollama
curl -X POST http://127.0.0.1:8000/api/v1/ai/embeddings \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"provider":"ollama","input":["hello world"]}'

# 结构化 JSON 输出
curl -X POST http://127.0.0.1:8000/api/v1/ai/chat \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"总结"}],"response_format":{"type":"json_object"}}'

# 流式（NDJSON）
curl -N -X POST http://127.0.0.1:8000/api/v1/ai/chat \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"讲个故事"}],"stream":true}'

# RAG：入库 + 问答（不同 provider 的向量不互通，检索按入库 provider 过滤）
curl -X POST http://127.0.0.1:8000/api/v1/ai/knowledge/ingest \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"provider":"qwen","title":"手册","content":"FastAPI 是一个高性能的 Python Web 框架…"}'
curl -X POST http://127.0.0.1:8000/api/v1/ai/knowledge/answer \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"provider":"qwen","query":"FastAPI 是什么？"}'
```

### 配置真实模型

编辑 `.env`（完整模板见 `.env.example`）：

```ini
# 默认 provider（请求不传 provider 时使用）
APP_LLM_DEFAULT_PROVIDER=qwen

# 千问 / 通义（DashScope OpenAI 兼容网关）
APP_LLM_QWEN_API_KEY=sk-xxx
# APP_LLM_QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
# APP_LLM_QWEN_CHAT_MODEL=qwen-plus
# APP_LLM_QWEN_EMBEDDING_MODEL=text-embedding-v3

# OpenAI
APP_LLM_OPENAI_API_KEY=sk-xxx

# DeepSeek
APP_LLM_DEEPSEEK_API_KEY=sk-xxx

# Ollama / MLX（本地，无需 Key，按需调 base_url）
# APP_LLM_OLLAMA_BASE_URL=http://localhost:11434/v1
# APP_LLM_MLX_BASE_URL=http://localhost:8080/v1
```

## 说明

- 配置统一放在 `app/config.py`，环境变量前缀为 `APP_`（如 `APP_DEBUG=true`）。
- 数据库：默认 SQLite（`sqlite+aiosqlite:///./app.db`），可用 `APP_DATABASE_URL` 切换到 PostgreSQL 等；迁移用 Alembic（异步）。
- 向量检索：Postgres 需 pgvector 扩展（`create extension if not exists vector`）；启动与迁移都会自动执行这句 IF NOT EXISTS，无权限时降级并记日志。
- 持久化：users / items / RAG 知识库（Postgres 存 pgvector 列 + HNSW 索引，SQLite 存 JSON）/ API Key / 用量日志全部落库，进程重启不丢数据。
- 管理面鉴权：权限码定义在 `app/rbac.py`，路由只声明 `Depends(deps.require_permission(Permission.XXX))`；
  新增接口只需选权限码，无需改鉴权中间件（详见「管理后台与 RBAC 权限」）。
- 代码风格由 `ruff` 检查（`ruff check .`），类型由 `mypy` 检查（`mypy app`）。

## 前端（Vue 3 + Ant Design Vue）

工程内附 `web-vben/`，用 **Vue 3 + Vite + Ant Design Vue 4** 实现控制台：
`概览 / AI 对话 / 嵌入向量 / 知识库 / API Key / 会话 / 账号管理 / 角色权限`，
构建产物直出到 `web/dist` 由后端单进程托管。

**当前前端能力**：

- **多轮对话**：聊天气泡、自动滚动、上下文记忆、手动停止、用量统计（prompt/completion/总 token）；
  支持「流式输出」与「非流式」切换，可选「结构化 JSON 输出」（mock 输出 JSON、真实 provider 返回结构化内容）。
- **Provider 感知**：provider 与模型名完全由后端 `.env` 决定（`GET /ai/providers` 只用于展示），
  前端不再传 `provider` / `model`。
- **顶部状态**：后端健康状态横幅 + 当前登录账号的角色标签；Key 栏可对现有 Key 自校验
  （`GET /ai/keys/me`，返 401 不会把控制台踢下线）。
- **Key 管理**：支持为 Key 命名、复制 plain key（仅显示一次）、设置每小时配额、查看本小时用量、吊销；
  无 `key:manage` 时自动降为只读。
- **RBAC 界面**：登录后拿 `roles/permissions` 渲染菜单（无权限的入口不显示），
  路由 `meta.permissions` 不满足时跳 `/403`；刷新页面以 `GET /admin/me` 为权威重拉权限。
  前端过滤只负责渲染，真正的拦截在后端 `require_permission`。

### 开发模式（推荐）

```bash
# 1. 后端（:8011，mock 或配置好真实 provider）
uvicorn app.main:app --reload --port 8011

# 2. 前端（:5173，/api 与 /admin 自动代理到 :8011）
cd web-vben
pnpm install
pnpm dev           # http://127.0.0.1:5173
```

打开 `http://127.0.0.1:5173`：空库时走「首次部署？创建第一个超级管理员」
（需填 `APP_ADMIN_API_KEY`），已有账号则直接用户名/密码登录。

> 若 `pnpm install` 报缓存权限问题，加 `--cache-dir /tmp/pnpm-cache`。

### 生产模式（单进程托管）

```bash
cd web-vben
pnpm build         # 输出到 ../web/dist/
cd ..
uvicorn app.main:app --port 8000
# 访问 http://127.0.0.1:8000  → FastAPI 自动托管 web/dist（/assets 静态 + SPA 回退）
```

`/api/*` 与 `/admin/*` 路由优先匹配，剩余路径回退到 `index.html`，
支持 history 模式的客户端路由。
