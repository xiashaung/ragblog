# FastAPI 全栈博客 · AI 知识库平台

一个前后端一体、单进程即可部署的完整项目：公开博客前台 + 管理控制台 +
AI 对话 / RAG 知识库 / 智能问数，后台管理面走一套 RBAC。
两个前端是独立 Vite 工程，构建产物由 FastAPI 一并静态托管。

## 功能一览

| 块 | 说明 |
| --- | --- |
| 博客前台（web-blog） | 文章 / 标签 / 分类 / AI 搜索（语义检索 + 带 `[n]` 引用的流式对话），挂站点根路径 `/`，即首页 |
| 管理控制台（web-vben） | 概览、AI 对话、知识库、博客管理、数据分析、系统管理（账号 / 角色 / 会话 / 模型），挂 `/admin` |
| AI 与 RAG | 多 provider 对话（OpenAI / DeepSeek / 通义 / Ollama / MLX / mock）、知识库三段式混合检索 + 精排、智能问数（Text-to-SQL） |
| RBAC | 权限码由代码定义（`app/rbac.py`），角色由数据库承载；菜单 / 按钮 / 弹窗 / 请求四层前端门控 + 后端 `require_permission` 真拦截 |

## 访问路径（生产单进程）

| 路径 | 内容 |
| --- | --- |
| `/` | 博客前台（`web-blog/dist`，vite base=`/`）；旧 `/blog/*` 链接 302 到对应新地址 |
| `/admin` | 管理控制台（`web/dist`，vite base=`/admin/`），客户端路由如 `/admin/roles` 刷新不 404 |
| `/api/v1/*` | 全部业务 API；管理 API 统一在 `/api/v1/admin/*`（`/admin` 路径只留给控制台页面，两者不串门） |
| `/api/v1/blog/*` | 博客公开接口（无鉴权，只出已发布文章） |
| `/media/uploads/*` | 上传图片等媒体文件（磁盘直出） |
| `/docs` | Swagger 交互式文档 |

## 技术栈

- 后端：FastAPI + SQLAlchemy 2（asyncio）+ Alembic + pydantic-settings；SQLite 默认，可切 PostgreSQL（pgvector + HNSW）
- 前端：Vue 3 + Vite + TypeScript + Ant Design Vue 4 + Pinia（pnpm 管理）
- 鉴权：Bearer token（http-only cookie 同步写），引导密钥 `APP_ADMIN_API_KEY` 仅用于首次建超管

## 目录结构

```
.
├── app/
│   ├── main.py              # 应用入口：中间件（envelope/CORS）→ 路由 → 静态托管（/、/admin、/media）
│   ├── config.py            # pydantic-settings 配置（.env，前缀 APP_）
│   ├── db.py                # 异步引擎 + Session 工厂
│   ├── models.py            # ORM 模型（账号/角色/会话/商品/RAG/分析/博客等记录）
│   ├── rbac.py              # 权限码目录 + 内置角色种子（权限的唯一来源）
│   ├── deps.py              # 鉴权依赖：require_admin_principal / require_admin_session / require_permission
│   ├── bootstrap.py         # CLI：python -m app.bootstrap seed（建首个超管）
│   ├── routers/             # 路由层（只处理 HTTP 细节）
│   │   ├── health.py        # /api/v1/health 健康检查
│   │   ├── users.py         # /api/v1/users CRUD（示例接口，不鉴权）
│   │   ├── items.py         # /api/v1/items CRUD（示例接口，不鉴权）
│   │   ├── ai.py            # /api/v1/ai/*（对话 + 知识库，知识库挂 knowledge:*）
│   │   ├── upload.py        # /api/v1/upload*（文件入库，knowledge:manage）
│   │   ├── analytics.py     # /api/v1/analytics/*（智能问数，analytics:read）
│   │   ├── blog.py          # 前台 /api/v1/blog（公开）+ 后台 /api/v1/admin/blogs（blog:*）
│   │   ├── datasources.py   # /api/v1/admin/data-sources（datasource:read/manage）
│   │   ├── system_config.py # /api/v1/system/config/*（模型选择与接入，system:read/manage）
│   │   └── admin.py         # /api/v1/admin/*（登录 + 账号 / 角色 / 会话，全部挂权限码）
│   ├── schemas/             # Pydantic 请求/响应模型（admin/ai/analytics/blog/datasource/envelope/item/upload/user）
│   └── services/            # 业务逻辑层（异步 DB 实现）
│       ├── admin_service.py / rbac_service.py  # 登录会话 / 权限解析 + 防提权守卫
│       ├── user_service.py / item_service.py   # 示例 CRUD
│       ├── llm.py / provider_store.py          # LLM 网关（多 provider 注册表）与接入配置内存态
│       ├── system_config.py                    # 库里存的系统配置（各功能选哪个 provider）
│       ├── rag.py / rag_queue.py / reranker.py # 分块 + 向量检索（pgvector/JSON 双路径）+ 异步索引队列 + 精排
│       ├── chat_history.py / chat_tools.py     # 会话持久化 + 联网检索 tools 循环
│       ├── web_search.py / web_page.py         # 联网搜索与网页抓取
│       ├── reasoning.py / markdown_text.py     # 思考链解析 / HTML→Markdown
│       ├── document_parser.py                  # pdf/docx 上传解析
│       └── blog.py / analytics.py / datasource.py  # 博客 / 问数 / 外部数据源
├── alembic/                 # 数据库迁移（异步）
├── tests/                   # pytest（httpx ASGI transport，mock LLM + 临时 SQLite）
│   ├── conftest.py          # client / console / account_factory fixture
│   ├── test_api.py          # 基础 CRUD
│   ├── test_ai.py           # AI / RAG（含分片策略测试）
│   ├── test_analytics.py / test_datasources.py
│   ├── test_blog.py / test_chat_sessions.py
│   ├── test_rbac.py         # 引导、权限矩阵、提权与锁死守卫
│   └── test_web_search_tools.py
├── web-vben/                # 管理控制台源码（vite base=/admin/，产物输出 ../web/dist）
├── web-blog/                # 博客前台源码（vite base=/，产物 web-blog/dist 挂站点根路径）
├── web/dist/                # 管理控制台构建产物（被 FastAPI 静态托管）
├── media/uploads/           # 上传的文件（经 /media 对外服务）
├── docs/                    # 文档（RAG 技术方案等）
├── run.py                   # 本地开发入口（uvicorn --reload :8011）
├── scripts/                 # 部署脚本：build.sh / deploy.sh / start.sh / stop.sh
├── pyproject.toml           # 依赖与 ruff/mypy/pytest 配置
├── .env.example             # 配置模板
└── README.md
```

## 快速开始

### 后端

```bash
# 创建虚拟环境并安装依赖（含 dev 组：pytest/ruff/mypy）
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 复制配置模板并按需修改
cp .env.example .env

# 启动开发服务器（默认 :8011 + reload；APP_HOST/APP_PORT/APP_RELOAD 可覆盖）
python run.py
```

交互式文档: http://127.0.0.1:8011/docs 。默认 SQLite（`./app.db`），
启动时自动建表；生产用 PostgreSQL 时跑 `alembic upgrade head`。

### 管理控制台（开发模式）

```bash
# 后端保持 :8011 运行
cd web-vben
pnpm install
pnpm dev           # http://127.0.0.1:5173/admin/ （/api 与 /media 代理到 :8011）
```

打开后空库时走「首次部署？创建第一个超级管理员」（需填 `APP_ADMIN_API_KEY`），
已有账号则直接用户名 / 密码登录。

> 若 `pnpm install` 报缓存权限问题，加 `--cache-dir /tmp/pnpm-cache`。

### 博客前台（开发模式）

```bash
cd web-blog
pnpm install
pnpm dev           # http://127.0.0.1:5174/ （/api 与 /media 代理到 :8011，与后台 5173 并行）
```

### 生产模式（单进程托管）

```bash
cd web-vben && pnpm build        # 输出 ../web/dist/
cd ../web-blog && pnpm build     # 输出 web-blog/dist/
cd .. && uvicorn app.main:app --port 8000
```

访问 `http://127.0.0.1:8000/`（博客首页）与 `http://127.0.0.1:8000/admin/`（控制台）。
匹配顺序：`/api/*`（含管理 API `/api/v1/admin/*`）与 `/media/*` 优先；`/admin/*`
回退到控制台 `index.html`；旧 `/blog/*` 302 到新地址；其余路径回退到博客前台
`index.html`——两个 SPA 的 history 路由刷新都不会 404。

### 部署脚本（scripts/）

| 脚本 | 用途 |
| --- | --- |
| `scripts/build.sh [all\|admin\|blog] [user@host:/repo]` | 构建两个前端；带第二参数时构建后 rsync 产物到服务器（服务器无 Node 时用） |
| `scripts/deploy.sh` | 服务器端一键部署：git pull → 同步依赖 → 迁移（钉住 `MIGRATION_REVISION`，原因见脚本头注释）→ 重建前端 → 重启 → 健康检查 |
| `scripts/start.sh` / `stop.sh` | 生产启停：单进程 uvicorn（勿 `--workers` 多开，RAG 队列会重复消费），pid 与日志在 `logs/` |

首次部署的手动准备步骤与 `.env` 生产要点清单见 `scripts/deploy.sh` 头部注释。

## 配置

全部配置在 `app/config.py`（pydantic-settings），环境变量前缀 `APP_`，完整模板见 `.env.example`。要点：

```ini
APP_DATABASE_URL=sqlite+aiosqlite:///./app.db   # 可切 postgresql+asyncpg://...
APP_DEBUG=true
APP_ADMIN_API_KEY=                              # 仅首次引导用，引导完成建议置空
APP_LLM_DEFAULT_PROVIDER=qwen                   # 请求不传 provider 时的兜底

# 千问 / 通义（DashScope OpenAI 兼容网关；其余 provider 同构）
APP_LLM_QWEN_API_KEY=sk-xxx
# APP_LLM_QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
# APP_LLM_QWEN_CHAT_MODEL=qwen-plus
# APP_LLM_QWEN_EMBEDDING_MODEL=text-embedding-v3

# Ollama / MLX（本地，无需 Key，按需调 base_url）
# APP_LLM_OLLAMA_BASE_URL=http://localhost:11434/v1
# APP_LLM_MLX_BASE_URL=http://localhost:8080/v1

# RAG：混合检索与精排开关（详见 docs/RAG技术方案.md）
# APP_RAG_HYBRID_ENABLED=true
# APP_RAG_RERANK_ENABLED=true

# 可观测性：统一日志（带请求 ID）与 /metrics 拉取令牌
# APP_LOG_LEVEL=INFO
# APP_METRICS_TOKEN=            # 公网暴露 /metrics 时必配

# LLM 瞬时故障重试与降级（只影响非流式对话；留空备胎 = 关闭）
# APP_LLM_RETRY_MAX_ATTEMPTS=3
# APP_LLM_FALLBACK_PROVIDER=deepseek

# 数据源连接串落盘加密（生产显式配；换 key 后存量密文需重录）
# APP_DATASOURCE_SECRET_KEY=

# 登录限流（IP+用户名滑窗；login 与 onboard 都受护）
# APP_LOGIN_MAX_ATTEMPTS=10
# APP_LOGIN_LOCK_THRESHOLD=5
# APP_LOGIN_LOCKOUT_SECONDS=900
```

### 安全与可观测默认值

- **健康与指标**：`/api/v1/health`（存活）、`/api/v1/health/ready`（真探 DB 与
  RAG Worker，不就绪返 503）、`/metrics`（Prometheus 文本格式，配了
  `APP_METRICS_TOKEN` 则需 `Authorization: Bearer`）。
- **请求追踪**：每个请求发 `X-Request-ID`（入站已带则沿用），日志行同字段可检索。
- **鉴权防线**：口令 argon2id；数据源连接串 Fernet 加密；登录/引导限流
  （超限 429 + `Retry-After`）；全响应带安全头（nosniff / X-Frame-Options /
  Referrer-Policy，HTTPS 下加 HSTS）。
- **SSRF 闸门**：URL 录入与 `fetch_page` 工具共用一道闸：只放 http(s)，
  DNS 解析后验 IP，重定向逐跳复检，内网/元数据地址请求根本不会发出。
- **上传上限**：单文档 20MB（分块读、超限即中止）、单图 5MB、批量一次≤20 件。
- **LLM 容错**：超时/断连/429/5xx 指数退避重试（SDK 内置重试已关，单层）；
  非流式对话主端打穿后可降级到备胎 provider；流式/嵌入不降级（语义上不能换）。

> 配置 `@lru_cache` 缓存，改 `.env` 后需重启进程；且「模型接入 / 系统配置」在
> 数据库里有运行时选择（后台改完即生效，不需要重启），`.env` 只是没配置时的兜底。

## 管理后台与 RBAC 权限

后台管理面（账号 / 角色 / 会话 / 知识库 / 博客 / 数据源 / 智能问数 / 模型配置）
走一套 RBAC：**权限码由代码定义，角色由数据库承载**。权限码唯一来源是
`app/rbac.py`（`Permission` 枚举 + `PERMISSION_CATALOG`，带中文名与分组），
角色是运营数据，可在「角色权限」页自由增删改。新增权限码只改代码，不需要迁移。

### 认证方式

```text
POST /api/v1/admin/login  {username, password}
  → {token, admin, roles, permissions}      # 同时写 http-only cookie: admin_token
后续管理接口带：Authorization: Bearer <token>
```

登录失效返回 `401`，权限不足返回 `403`（message 为中文，形如
`权限不足，需要：管理角色（role:manage）`），尝试过频返回 `429`（带
`Retry-After` 秒数：同 IP+用户名连错 5 次锁 15 分钟，引导密钥猜测同样受护）。

### 权限码与内置角色

| 分组 | 权限码 | 含义 |
| --- | --- | --- |
| 账号管理 | `account:read` | 查看后台账号与角色 |
| 账号管理 | `account:manage` | 新建 / 停用 / 删除账号、重置密码、分配角色 |
| 角色权限 | `role:read` | 查看角色与权限目录 |
| 角色权限 | `role:manage` | 新建 / 修改 / 删除角色（提权敏感，仅超管可授予） |
| 会话 | `session:read` | 查看在线会话 |
| 会话 | `session:revoke` | 强制下线某个会话 |
| 博客 | `blog:read` / `blog:manage` | 文章 / 分类的读与写（发布自动同步进知识库） |
| 数据分析 | `analytics:read` | 智能问数（只读 Text-to-SQL） |
| 数据分析 | `datasource:read` / `datasource:manage` | 外部数据源查看（连接串只回掩码）/ 增删改（含凭证，高敏感） |
| 模型接入 | `system:read` / `system:manage` | 查看 / 切换各功能 provider 与模型接入 |
| 知识库 | `knowledge:read` / `knowledge:manage` | 浏览与问答 / 录入、上传、重建、删除 |

**写权限隐含同一资源的读权限**：规则集中在 `rbac.py::MANAGE_IMPLIES_READ`，由
`grants()` 统一生效（所以只勾 `xxx:manage` 也能看得懂列表），前端
`store/modules/auth.ts` 里有同表副本，保证菜单 / 路由 / 按钮三处口径一致。
`session:revoke` 不算 `session:read` 的写档（它不是同一资源的写操作），运维角色本就同时持有两个。

| 内置角色 | 权限 |
| --- | --- |
| `superadmin` | `*`（全部权限，恒不可改，只有这一个角色能持有 `*`） |
| `admin` | 账号读写 + `role:read` + 会话读写 + `analytics:read` + `datasource:read` + 知识库读写 |
| `operator` | `session:read` + `session:revoke` |
| `viewer` | `account:read` + `role:read` + `session:read` + `knowledge:read`（只读） |

模型接入（`system:*`）两个码**不预发给任何内置角色**：`GET /system/config/providers`
目前会把接入里的 API Key 明文回显，接口做好掩码前不该交给只读角色；
现阶段只有 `superadmin` 靠 `*` 能用，之后在「角色权限」页按需分配。
内置角色不可删除、标识不可改；一个账号可挂多个角色，权限取并集。

> 这张表是权限的唯一定义，但角色数据在库里，而 `ensure_builtin_roles` 只在**首次引导**
> 时跑。所以改动内置角色的权限集后，已有库要手动升级一次：
> `alembic upgrade h7i8j9k0l1m2`（这版会按代码重刷四个内置角色，顺带清掉 API Key 时代
> 留下的 `key:*`）。这里故意写具体版本号：项目当前有两个 head，
> `g6h7i8j9k0l1`（system_configs）是从 `c2a3b4d5e6f7` 分出去、实际没应用过的孤儿分支
> （表由 `create_all` 建），所以 `upgrade head` 会报歧义、`upgrade heads` 会去重建已存在的表。

### 端点与所需权限

| 方法 | 路径 | 所需权限 |
| --- | --- | --- |
| POST | /api/v1/admin/onboard | 公开 + 引导密钥 |
| POST | /api/v1/admin/login /logout /refresh | 登录 / 会话 |
| GET | /api/v1/admin/me | 登录（任何权限） |
| GET | /api/v1/admin/sessions | `session:read` |
| DELETE | /api/v1/admin/sessions/{id} | `session:revoke` |
| GET | /api/v1/admin/permissions、/api/v1/admin/roles | `role:read` |
| POST / PATCH / DELETE | /api/v1/admin/roles/{id} | `role:manage` |
| GET | /api/v1/admin/accounts | `account:read` |
| POST / PATCH / DELETE | /api/v1/admin/accounts/{id} | `account:manage` |
| PUT | /api/v1/admin/accounts/{id}/roles | `account:manage` |
| GET | /api/v1/admin/blogs/*（文章 / 分类列表与详情） | `blog:read` |
| POST / PATCH / DELETE | /api/v1/admin/blogs/* | `blog:manage` |
| GET | /api/v1/ai/knowledge、/stats、/{id}、/search、/answer | `knowledge:read` |
| POST | /api/v1/ai/knowledge/ingest、/ingest-pairs、/fetch-url | `knowledge:manage` |
| PATCH / DELETE | /api/v1/ai/knowledge/{id}、/batch-delete、/reindex | `knowledge:manage` |
| POST | /api/v1/upload、/upload/batch、/upload/image | `knowledge:manage` |
| GET | /api/v1/system/config/*（各类 GET） | `system:read` |
| PATCH / POST / DELETE | /api/v1/system/config/*（写） | `system:manage` |
| GET | /api/v1/admin/data-sources | `datasource:read` |
| POST / PATCH / DELETE / POST /test | /api/v1/admin/data-sources | `datasource:manage` |
| ALL | /api/v1/analytics/* | `analytics:read` |
| ALL | /api/v1/ai/chat | 公开（无鉴权，供外部调用） |
| ALL | /api/v1/ai/chat/sessions* | 登录（只作用于自己的会话） |
| GET | /api/v1/blog/* | 公开（只出已发布文章） |

### 前端按钮级权限

同一个权限码在前端要同时管四件事，入口分别是（前一层管不住后一层）：

- **菜单 / 路由**：`router/index.ts` 的 `meta.permissions` + `MainLayout` 的菜单过滤（无权不显示）；
- **按钮**：全局指令 `v-perm="'knowledge:manage'"`（`src/directives/perm.ts`）。无权时不摘节点，
  而是置灰 + 悬停说明缺哪条（中文名单在 `src/config/permissions.ts`，与后端 catalog 同一口径）；
  需要「任一命中」用 `v-perm-any`。已经被 `a-tooltip` 包住的按钮不能用指令（原生 title 会被盖掉），
  改写 `:disabled="!canManage"` + `:title="canManage ? 正常文案 : permHint(code)"`；
  弹窗底部的「确定」由 `a-modal` 自己渲染，指令够不着，用 `usePerm()` 的 `okProps(code)`
  把 `disabled` 递进 `ok-button-props`（可叠 `{ danger: true }`）；
- **提交兜底**：禁了按钮仍拦不住回车提交与「页面开着时权限被收」，所以每个写处理函数
  开头再问一次：`if (!canManage.value) return message.warning(need(code))`；
- **请求**：`usePerm()` 的 `can()` 用于脚本判断，没权限就干脆不发那个请求（否则只收到一个 403，
  页面还白转圈）——首页「概览」就是按这个取数的。

渲染层这一套只管交互观感，绕过也过不了后端的 `require_permission`。

### 防提权 / 防锁死守卫

集中在 `app/services/rbac_service.py`，路由层不管这些：

- 非超级管理员不能创建/修改含 `role:manage` 的角色，也不能把这类角色分配给账号；
- `*` 只能属于内置 `superadmin`（否则可以伪造一个“隐藏的超管”绕过下面条规则）；
- 不能停用、删除、或收回**自己**的超管角色，也不能移除最后一个可用超管的超管角色；
- 停用账号 / 删号 / 重置密码会同时吊销该账号的全部在线会话；
- 有成员的角色不能删除，内置角色不能删除。

### 首次引导

`APP_ADMIN_API_KEY` 现在**只有一个用途**：作为 `X-API-Key` 头证明调用方是运维本人，
抢注库里第一个超管（`POST /api/v1/admin/onboard`）。它不再是任何管理接口的旁路，
也不再生效于配额豁免。引导完成后建议置空以关闭 HTTP 入口，改用 CLI：

```bash
# 方式一（需 APP_ADMIN_API_KEY）：控制台页面 /admin/auth/onboard 或 curl
# 方式二（推荐，不开放 HTTP 入口）：
python -m app.bootstrap seed --username admin --password 'xxx'
```

> 不要直接往 `admin_users` 插行：没有角色绑定的账号登录后哪个管理接口都进不去。
> 升级时迁移（`f5a6b7c8d9e0`）会把一切现存账号绑到 `superadmin`，语义与升级前的
> 「唯一管理员」等价，不会降权。

> 尚未纳入 RBAC：`/api/v1/users`、`/api/v1/items` 仍为开放示例接口；`/api/v1/ai/chat`
> 不鉴权（供外部调用）；「对话会话」那组接口只要求后台登录态
> （`require_admin_session`），因为它们只作用于调用者自己。

## AI 接口（对话 + 知识库 RAG）

封装了一套面向 AI 调用的接口，**支持在请求级别切换多家模型平台**，每家平台有独立的
`api_key` / `base_url` / 模型名配置。请求体里传 `provider` 字段即可切换；不传则回退到
`APP_LLM_DEFAULT_PROVIDER`。实际用哪个端点与模型名以「系统配置」页（数据库）为准，`.env` 兜底。

### 可选 Provider

| 名称 | 说明 | 需要 Key | 默认 base_url | 默认 chat_model | 默认 embedding_model |
| --- | --- | --- | --- | --- | --- |
| `openai` | OpenAI 官方（及任意 OpenAI 兼容网关） | 是 | `https://api.openai.com/v1` | `gpt-4o-mini` | `text-embedding-3-small` |
| `deepseek` | DeepSeek（OpenAI 兼容） | 是 | `https://api.deepseek.com/v1` | `deepseek-chat` | `deepseek-embed` |
| `qwen` | 阿里通义 / DashScope（OpenAI 兼容） | 是 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` | `text-embedding-v3` |
| `ollama` | 本地 Ollama 服务器 | 否 | `http://localhost:11434/v1` | `llama3.1` | `nomic-embed-text` |
| `mlx` | 本地 MLX-LM 服务器 | 否 | `http://localhost:8080/v1` | `mlx-community/llama-3.1-8b-instruct` | `mlx-community/bge-small-en-v1.5` |
| `mock` | 内置离线后端（回显 + 哈希向量，无网络） | 否 | — | `mock-chat` | `mock-embed` |

前 5 家共用同一个 OpenAI 兼容客户端，只有 `api_key` / `base_url` / 模型名不同；
`mock` 是确定性的离线后端，供开发 / 测试使用。

### 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | /api/v1/ai/chat | 对话补全，支持流式（NDJSON）/结构化 JSON 输出，可挂联网检索 tools |
| POST | /api/v1/ai/knowledge/ingest | RAG：录入文本（`format`: text/markdown/html，自动分块 + 向量化） |
| POST | /api/v1/ai/knowledge/ingest-pairs | RAG：录入一批问答对（合为一篇 FAQ） |
| POST | /api/v1/ai/knowledge/fetch-url | RAG：抓取网页正文后入库 |
| POST | /api/v1/upload | RAG：单文件入库（pdf/docx/txt/md/csv/log） |
| POST | /api/v1/upload/batch | RAG：批量文件入库（≤20 个，逐文件返回成败） |
| POST | /api/v1/upload/image | 编辑器插图：只落盘拿 `/media/uploads/...` 地址，不建文档 |
| GET | /api/v1/ai/knowledge | RAG：文档列表（关键字 / provider / 仅待重建 / 分页） |
| GET | /api/v1/ai/knowledge/stats | RAG：文档·分片·字符统计与 provider 明细 |
| GET | /api/v1/ai/knowledge/{id} | RAG：文档详情（原文 + 分片） |
| PATCH | /api/v1/ai/knowledge/{id} | RAG：改名 / 换正文（换正文会重新向量化） |
| POST | /api/v1/ai/knowledge/{id}/reindex | RAG：用当前默认 provider 重建向量 |
| DELETE | /api/v1/ai/knowledge/{id} | RAG：删除文档 |
| POST | /api/v1/ai/knowledge/batch-delete | RAG：批量删除，返回 deleted / failed |
| POST | /api/v1/ai/knowledge/search | RAG：向量检索，返回引用片段 |
| POST | /api/v1/ai/knowledge/answer | RAG：检索 + 带引用的回答 |
| GET | /api/v1/ai/providers | 各 provider 的默认配置（供前端展示） |
| GET | /api/v1/analytics/* | 智能问数（Text-to-SQL，只读） |

### 约定与坑

- 不同 provider 的向量不可比：换 `APP_LLM_DEFAULT_PROVIDER` 后，旧文档会被标记为
  `stale`（列表接口 `only_stale=true` 可单独筛出），需调 `reindex` 重建。
- 正文只存一种格式（Markdown 源码或纯文本）：录入时的 `format=html` 会先由
  `app/services/markdown_text.py`（stdlib `html.parser`，无新依赖）转成 Markdown 再入库，
  文档 meta 里只记 `text` / `markdown`；PATCH 不传 `format` 则沿用文档原格式。
- 向量存储双路径：Postgres 下 `knowledge_chunks.vector` 是 pgvector 的
  `vector(<APP_RAG_VECTOR_DIM>)` 定长列，相似度由库内余弦距离 `<=>` 计算并走 HNSW
  索引；SQLite 下同一列退化为 JSON，改由 Python 算余弦（测试与轻量开发无需扩展）。
  换 embedding 模型导致维度变化时，改 `APP_RAG_VECTOR_DIM` 并重跑
  `alembic upgrade head`：维度不符的旧向量会先备份进 `knowledge_chunks_vector_backup`
  再置空（统计接口的 `vectorless_chunks` 会报出来），之后对文档重建索引。
- 分片策略：Markdown 按标题结构感知切分（每片自带「文档标题 > 节 > 小节」前缀），
  超长段落按句子边界切，overlap 随 chunk_size 比例缩放；详见 `docs/RAG技术方案.md`。

### 示例

```bash
# 先登录控制台，后面的 AI / 知识库接口都用这个 Bearer token
TOKEN=$(curl -s -X POST http://127.0.0.1:8011/api/v1/admin/login -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"xxx"}' | sed -n 's/.*"token":"\([^"]\+\)".*/\1/p')

# 对话（走默认 provider；此接口本身不鉴权）
curl -X POST http://127.0.0.1:8011/api/v1/ai/chat \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"你好"}]}'

# 指定 provider + 覆盖模型
curl -X POST http://127.0.0.1:8011/api/v1/ai/chat \
  -H 'Content-Type: application/json' \
  -d '{"provider":"deepseek","model":"deepseek-reasoner","messages":[{"role":"user","content":"1+1?"}]}'

# 流式（NDJSON）
curl -N -X POST http://127.0.0.1:8011/api/v1/ai/chat \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"讲个故事"}],"stream":true}'

# RAG：入库需 knowledge:manage，问答需 knowledge:read（不同 provider 的向量不互通）
curl -X POST http://127.0.0.1:8011/api/v1/ai/knowledge/ingest \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"provider":"qwen","title":"手册","content":"FastAPI 是一个高性能的 Python Web 框架…"}'
curl -X POST http://127.0.0.1:8011/api/v1/ai/knowledge/answer \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"provider":"qwen","query":"FastAPI 是什么？"}'
```

## 个人博客

### 前台（web-blog/）

独立的 Vue 3 + Vite SPA，走公开接口 `/api/v1/blog/*`（无鉴权，只出已发布文章），
挂站点根路径 `/`。页面：首页（横向 Hero + 封面卡片网格，分类/标签收进顶栏下拉）、
文章页、标签页、分类页（`/category/:slug`）与 AI 搜索页（单输入框：提问即并行完成
语义检索 + 带 `[n]` 引用的多轮流式对话，会话管理持久化在浏览器 localStorage）。
文章页自带阅读体验件：章节目录（TOC）、预计阅读时长、代码块一键复制、
上/下篇与相关文章推荐（`GET /posts/{slug}/neighbors`、`GET /posts/{slug}/related`，均不计阅读数）。

### 后台创作（控制台 → 博客管理）

「文章管理 / 分类管理」读写需 `blog:read` / `blog:manage`；每篇最多归属一个分类
（也可不归类），删分类前需先重新归类其下文章。**发布 = 同步进知识库**
（`kind: "blog"` 文档，复用三段式混合检索），下线/删除会连带删掉索引。
发布/问答依赖「系统配置」里的 embedding_provider 与 rag_provider，与知识库同一套要求。

> 挂载约定：`web-blog/dist` 存在时，后端在根路径 `/` 提供静态资源与 SPA 回退
> （`vite base` 与 `createWebHistory` 都是 `/`，两者必须保持一致）；旧 `/blog/*`
> 链接 302 重定向到对应新地址；博客公开接口在 FastAPI 层先于 SPA 回退匹配，互不影响。

## 控制台界面能力（web-vben）

菜单按「两个顶级页 + 三个分组」排（项仍按权限码过滤，无权不显示）：
`概览`、`AI 对话` 为顶级；分组 `知识库`（文档列表 / 内容录入 / 检索问答）、
`数据分析`（智能问数 / 数据源管理）、`系统管理`（账号管理 / 角色权限 / 会话 / 模型选择 / 模型接入）。

- **多轮对话**：聊天气泡、自动滚动、上下文记忆、手动停止、用量统计
  （prompt/completion/总 token）；「流式 / 非流式」可切，可选「结构化 JSON 输出」。
- **Provider 感知**：provider 与模型名完全由后端「系统配置」决定
  （`GET /ai/providers` 只用于展示），前端不再传 `provider` / `model`。
- **顶部状态**：后端健康状态横幅 + 当前登录账号的角色标签
  （`session:read` 才会拉在线会话数）。
- **RBAC 界面**：登录后拿 `roles/permissions` 渲染菜单（无权限的入口不显示），
  路由 `meta.permissions` 不满足时跳 `/403`；刷新页面以 `GET /api/v1/admin/me`
  为权威重拉权限。按钮级详见上面「前端按钮级权限」。
- **Markdown 录入与展示**：「内容录入」的文本 tab 是自研编辑器 `MarkdownEditor.vue`
  （工具栏 + 编辑/分栏/预览三态 + 插图与粘截图上传，零新增依赖）；格式可选
  纯文本 / Markdown / HTML（HTML 由后端转成 Markdown）。文档详情默认按语法渲染
  （`RichText.vue` + `utils/rich.ts`：标题/列表/引用/代码块/表格/删除线，先转义后白名单），
  点「编辑源码」才切回编辑器；列表里 Markdown 文档带 `MD` 标签、摘要剔掉语法符号。
- **明暗主题**：两套 CSS 变量集中在 `[data-theme]` 单点管理（含侧栏），
  组件一律 `var(--xxx)`，禁止硬编码明暗色；前台 web-blog 同一套精神。

## 测试与代码质量

```bash
pytest                    # 全量测试（mock LLM + 临时 SQLite，无需外部服务）
ruff check .              # 代码风格（line-length=100）
mypy app                  # 类型检查（strict）
```

## 其他说明

- 统一响应信封：所有 2xx JSON **对象**响应被 `_EnvelopeMiddleware` 包成
  `{code, data, message}`（裸数组/标量原样过，流式 NDJSON 与 HTML 透传），
  错误体由 `app/responses.py` 的异常处理器拼好，格式同一。
- 持久化：后台账号 / 角色 / 会话 / items / RAG 知识库（Postgres 存 pgvector 列 + HNSW
  索引，SQLite 存 JSON）/ 对话历史 / 博客文章 / 问数记录全部落库，进程重启不丢数据。
- 数据库：默认 SQLite（`sqlite+aiosqlite:///./app.db`），可用 `APP_DATABASE_URL`
  切换到 PostgreSQL 等；迁移用 Alembic（异步）。
- 向量检索：Postgres 需 pgvector 扩展（`create extension if not exists vector`）；
  启动与迁移都会自动执行这句 IF NOT EXISTS，无权限时降级并记日志。
- 管理面鉴权：权限码定义在 `app/rbac.py`，路由只声明
  `Depends(deps.require_permission(Permission.XXX))`；新增接口只需选权限码，
  无需改鉴权中间件（详见「管理后台与 RBAC 权限」）。
- 图片一律落盘 `media/uploads/` 并经 `/media` 静态服务存 URL，数据库不存 base64。
