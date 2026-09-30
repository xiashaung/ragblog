# RAG 检索的设计与流程（结合代码）

> 面向"想搞懂这套检索怎么工作、代码在哪"的开发者。
> 用「去图书馆找书」的比喻，从**为什么这么设计**讲到**代码怎么实现**。
> 原理全篇见同目录 `RAG技术方案.md`；本文聚焦**检索**这一段，并串起入库 → 检索 → 作答的完整链路。

核心代码全部在 `app/services/rag.py`，下面重点走这几个函数：

```
chunk_document()       ← 切块，把长文撕成卡片（rag.py:200）
upsert_document()      ← 入库总入口（rag.py:477）
search()               ← 总指挥，串联全流程（rag.py:1255）
_search_pgvector()     ← 向量召回腿，PostgreSQL 版（rag.py:1085）
_search_in_python()    ← 向量召回腿，SQLite 版（rag.py:1120）
_search_lexical_pg()   ← 词法召回腿，pg_trgm（rag.py:1145）
_rrf_fuse()            ← 融合两条腿（rag.py:1182）
_rerank_hits()         ← 精排（rag.py:1227）
answer()               ← 拼 Context 交给对话模型（rag.py:1466）
```

---

## 一、先搞懂：为什么要"检索"

RAG = **R**etrieval **A**ugmented **G**eneration = 检索增强生成。

核心思想一句话：**不让 AI 凭记忆硬答，先找到资料再答。**

为什么？AI 训练知识有截止时间、还容易"瞎编"（幻觉）。查"我们公司报销制度"这种事它不可能背过。于是拆成两步：

> **① 检索 Retrieval**：先从数据库找最相关的几段文字
> **② 生成 Generation**：把这几段当"参考资料"交给 AI，让它照答

RAG 检索要解决的核心问题就是：**你的一句话，怎么从几万段文字里挑出最相关的几条？**

---

## 二、基础概念：向量（embedding）

这是整条链路的数学基础：

> 把一段文字喂给 embedding 模型，它吐出一串固定长度的数字（本项目是 1024 个）。
> **意思相近的文字，这串数字也相近。**

于是"找相关内容"退化成"找数字最接近的卡片"——这就是**向量（embedding）**。

每段文字入库时就算好向量，存在 `knowledge_chunks.vector` 字段（`app/models.py:120`）：

```python
# app/models.py:117-122
text: Mapped[str] = mapped_column(Text, nullable=False)
# 维度由 APP_RAG_VECTOR_DIM 决定。NULL 表示「有分片但没有可用向量」
vector: Mapped[list[float] | None] = mapped_column(
    _VECTOR_TYPE, default=list, nullable=True
)
```

`_VECTOR_TYPE` 是个"双面胶"（`app/models.py:75`）：

```python
_VECTOR_TYPE = Vector(get_settings().rag_vector_dim).with_variant(JSON(), "sqlite")
```

- **Postgres**：真 pgvector 列，余弦距离在**库内**算、可建 HNSW 索引——图书馆的"按内容指纹排好的书架"；
- **SQLite**：同一列退化成 JSON，打分回到 Python 内存里算——书架没有，把卡片全摊桌上逐张比对。测试和轻量开发不用装扩展。

维度是结构性参数，`rag_vector_dim = 1024`（`app/config.py:243`），只改 `.env`、改列要重建库。

---

## 三、建库第一步：切块（chunking）——把书撕成卡片

**为什么要切块？** 三个理由：

1. embedding 模型对输入长度有上限；
2. 一整篇文档压成一个向量会"语义稀释"——讲十件事的文档，哪件事都表达不浓；
3. `top_k` 想返回的是"几段话"，不是"几本书"，粒度必须细到段落级。

入口是 `chunk_document()`（`rag.py:200`），底层按格式分两条路：

### 3.1 纯文本：`chunk_text()`（rag.py:127）

- 按**段落/句子边界**打包，攒到 `chunk_size`（默认 500 字符，`DEFAULT_CHUNK_SIZE`，`rag.py:57`）为止 flush；
- 单段超长先按句末标点切句再打包（`_split_long_paragraph`，`rag.py:97`），**完全没有标点的长串**（CSV、日志）才定长硬切——保证一定有产出；
- 相邻块之间留**重叠尾巴**：

```python
if overlap is None:
    overlap = max(16, chunk_size // 10)   # 默认 1/10，太小语义断裂，太大稀释
```

给小白的理由：某句话正好横跨两块时，两块都能看到它，不会"上半句在 A 卡、下半句在 B 卡"谁都看不懂。

### 3.2 Markdown：先按标题切节（`split_markdown_sections`，rag.py:165）

用栈维护标题层级（`###` 前面有 `##` 时路径是两层，代码围栏里的 `#` 不算标题），每节单独走 `chunk_text`，并且**给每片开头拼上「文档标题 > 节 > 小节」的路径前缀**。

为什么？卡片被撕下来单独 embedding 时，得知道它出自哪一章，否则一句"它支持热更新"脱离上下文谁也不知道"它"是谁。前缀从预算里扣字符，剩余预算不足 `MIN_PREFIX_BUDGET = 64` 就不加（`rag.py:200` 附近）。

### 3.3 FAQ 特例：问句领块（`pairs_to_text`，rag.py:560）

```python
f"{FAQ_ASK_PREFIX}{question}\n{FAQ_ANSWER_PREFIX}{answer}"   # "问：…答：…"
```

把**问句**放每块开头，让这片的 embedding 主要由"用户真正会输入的措辞"决定（答案往往很长，会把关键词稀释掉）。这是"查询对齐"思想的廉价实现。

---

## 四、建库第二步：入库（算向量 + 落库）

所有录入入口——裸文本 `ingest()`（rag.py:545）、问答对 `ingest_pairs()`（rag.py:572）、URL 抓取 `ingest_from_url()`（rag.py:596）、文件上传——最后都汇到唯一的总入口 **`upsert_document()`（rag.py:477）**。四条路各自写 ID 规则、元数据、重建时机太容易漂移，收敛成一个函数是刻意的工程决定。

关键设计点：

- **同标题 = 同 ID = 替换而不是堆新的**：`_doc_id_for()`（rag.py:472）用 `md5(title)` 派生固定 `kb-xxxx` ID；重复录入先整体删除再重建，旧向量不残留。
- **先归一化再切块**：`normalize_content(content, fmt)`，HTML 转 Markdown，库里只存一种格式。
- **`replace_vectors()`（rag.py:422）的顺序有讲究**：先 chunk → 批量 embedding → **校验维度** → 才删旧分片、写新分片。

```python
# 先算出新向量再删旧分片：维度不符（或 embedding 失败）时，
# 文档不会被削成一个没有向量的空壳。
```

- **状态机**：文档带 `status`（`pending / processing / indexed / failed`），建好向量才置 `indexed`。**只有 indexed 的文档参与检索**（`rag.py:1285` 的 `where(KnowledgeDoc.status == INDEXED)`）——pending/failed 的向量属于旧模型或还没算完，搜了也没意义。
- **换嵌入模型 = 全库重建**：`mark_docs_for_full_rebuild()`（rag.py:793）把全部 `indexed` 拉回 `pending`，`reindex_all_docs()`（rag.py:816）逐篇重建。不同模型的向量**不可比、也不会共存**（同一列装不下两种维度）。
- **录入是慢活，走异步队列**：embedding 是网络调用，`app/services/rag_queue.py` 把上传/重建放进 `rag_tasks` 表 + 内存队列，HTTP 立即返回 `task_id`，前端轮询 `/knowledge/tasks` 看进度。

一句话总结建库链路：

```
取文本（文件/裸文本/问答对/URL）→ upsert_document → chunk_document 切块
→ 批量 embedding → 写 knowledge_chunks → status = indexed
```

---

## 五、检索主线：`search()`（rag.py:1255）逐段带读

docstring 就是提纲：**问句转向量 → 召回 →（融合）→（精排）→ 拼引用**。
三个开关决定走哪条路（都以数据库/`.env` 为准）：

```python
rerank_on = settings.rag_rerank_enabled and bool(settings.rag_rerank_base_url)
hybrid_on = settings.rag_hybrid_enabled and on_pg        # 词法腿需要 pg_trgm
recall_k  = max(payload.top_k * settings.rag_rerank_recall_multiplier, payload.top_k) \
            if (rerank_on or hybrid_on) else payload.top_k
```

### 5.1 查询转向量

```python
backend = llm.get_llm(payload.provider, feature="embedding")
query_emb = (await backend.embeddings(EmbeddingRequest(input=[payload.query]))).items[0].embedding
```

**必讲的坑**：查询和文档必须"同一个嵌入模型、同一个空间"，夹角才有意义。所以这里锁的是 embedding 后端，绝不能跟着回答模型走（`answer()` 里的注释专门解释过：改了 `rag_provider` 不能导致查不到文档）。

### 5.2 向量召回（最基础的 RAG）

Postgres 走库内 pgvector（`_search_pgvector`，`rag.py:1085`）：

```python
distance = KnowledgeChunk.vector.cosine_distance(query_emb)   # <=> 是余弦距离
... .where(KnowledgeChunk.vector.is_not(None))                # NULL 向量跳过
    .order_by(distance).limit(top_k)                          # 1 - distance 就是相似度
```

SQLite 退化成把候选拉进内存逐个 `_cosine` 打分排序（`_search_in_python`，`rag.py:1120`）。两条路**分数口径一致**（都是 0~1 的余弦相似度），上层不用关心库。查询向量维度不对直接报错，不做近似匹配。

### 5.3 词法召回：加第二条腿（`_search_lexical_pg`，rag.py:1145）

向量的弱点是**精确术语、代码标识符、低频词**——会被"抹平"成语义近似（搜 `RRF` 给你一堆"融合"）。所以叠一条 pg_trgm 三元组相似度腿：

```python
sim = func.word_similarity(query, KnowledgeChunk.text)
```

选 `word_similarity` 而不是 `to_tsvector`：前者拿文档里匹配最好的连续窗口比短查询，正好是"关键词查法"的形状，而且**不需要分词器就能命中中文**（`to_tsvector` 会把不带空格的中文当单 token）。整条腿包在 try/except 里：库里没装扩展，检索静默退成纯向量。

### 5.4 RRF 融合（`_rrf_fuse`，rag.py:1182）

两条腿的分数量纲不同（余弦 0~1 vs 三元组分），没法直接加权。RRF 的聪明之处：**只看名次，不看分数**：

```python
score(chunk) = Σ_腿 weight_腿 / (k + rank_腿)     # rank 从 1 开始，k=60
```

两腿都命中的分片自然上浮；去重键是 `doc_id + 完整文本`。展示用的 `score` 优先保留余弦（可比的 0~1 数），前端进度条才读得懂。

### 5.5 宽进严出 + 精排

注意召回数量不是 `top_k` 而是 `recall_k`（默认 5 倍）——**只要下游有重排者（融合/精排）就先多捞候选**，最后再裁到 `top_k`；两个开关都没开时行为与最朴素的单段式完全一致。

精排用 cross-encoder（`_rerank_hits`，rag.py:1227 + `app/services/reranker.py:28`）：把（查询, 文档）**成对**送进模型一起打分，比"各算各的向量再算夹角"看得细。打比方：向量召回像按书名找书，精排像请老师逐页批阅。三个细节：

- 排序跑在**完整分片文本**上（每篇截 2000 字符），不是截断预览，否则精度会掉；
- 分数只用来排序和卡 `rag_rerank_min_score` 阈值（标度因模型而异），**存下的展示分仍是余弦**；
- 任何失败/未开启 → 返回 `None`，调用方保持原序——**精排服务挂掉绝不能把检索一并带坑**。

### 5.6 组装引用

裁到 `top_k` 后，把命中文档 `meta["images"]` 里的图片 URL 旁挂在 `Citation` 上（文字描述已在分片里，图片供前端展示），片段截 `SNIPPET_CHARS = 500` 字符，产出 `KnowledgeSearchResponse(items=[Citation(index, knowledge_id, source, score, snippet, images)])`。

> 另有「文搜图」支线 `search_images()`（rag.py:1368）：查询文本走 `embed_images`（与图片入库同一 VL 模型、同端点、同维度），拿文字向量比图片向量——跨模态的关键同样是"查与存同空间"。

---

## 六、生成：检索结果怎么"喂"给模型（`answer()`，rag.py:1466）

三行核心，适合作为收尾演示：

```python
context = "\n".join(f"[{c.index}] {c.snippet}" for c in search_resp.items)
question = f"Context:\n{context}\n\nQuestion: {payload.query}"
content, ... = await backend.chat([ChatMessage(role="system", content=RAG_SYSTEM_PROMPT),
                                   ChatMessage(role="user", content=question)])
```

配合 `RAG_SYSTEM_PROMPT`（rag.py:1454）的四条规则：

1. 整合片段、用自己的话组织，不许照抄；
2. 长回答用 Markdown 小标题/列表；
3. 引用句末标注 `[n]` 来源编号；
4. **只依据 Context**，没有就明说「知识库中未找到相关内容」，不要编造。

返回 `answer + citations`，前端能展示出处——这就是"开卷考试要写参考答案页码"。调用方带图时走多模态 parts，视觉模型可看图作答。

---

## 七、一张表看全部开关与降级路径

| 增强层 | 开关（`app/config.py`） | 不满足/失败时 |
|---|---|---|
| pgvector 库内检索 | 库类型决定 | SQLite → Python 内存打分（口径不变） |
| 混合检索（词法腿） | `rag_hybrid_enabled`（默认 off，`config.py:276`） | 无 pg_trgm → try/except 退纯向量 |
| RRF 参数 | `rag_hybrid_rrf_k=60`、`rag_hybrid_vector_weight=0.5`、`rag_hybrid_text_topk=20` | — |
| 精排 | `rag_rerank_enabled`（默认 off）+ `rag_rerank_base_url`（`config.py:260-269`） | 服务挂/超时 → 保持余弦序；`min_score` 全滤掉是合法结果，但保底留一条 |
| 宽召回倍数 | `rag_rerank_recall_multiplier=5`（`config.py:266`） | 无下游重排时 = 原 top_k |

任何"增强层"坏掉，系统都退回上一层最朴素的形态继续工作，而不是整体报错。

---

## 八、带走的三条设计心法

1. **处处降级兜底**：pgvector→Python、trigram→纯向量、rerank→原序。每一层增强都必须"可静默缺席"。
2. **向量空间的纪律**：同模型同维度才可比；维度写死在列定义；换模型=全库置 pending 重建；只有 `indexed` 参与检索。这套状态机是"结果正确性"的地基。
3. **防幻觉靠流程不靠祈祷**：切块对齐 + 混合召回 + 精排解决"找得准"；prompt 四条规则 + 引用编号解决"即使找错也不编"。

---

## 九、给小白作者的 30 分钟带读顺序

1. **先跑一遍**（比看图快）：`POST /knowledge/ingest` 录一篇 → 轮询 `/knowledge/tasks` 等 status 变 `indexed` → `POST /knowledge/search` 看命中与分数 → `POST /knowledge/answer` 看带 `[n]` 引用的答案（路由在 `app/routers/ai.py`，查资料要 `knowledge:read` 权限）。
2. **投屏讲义**：`rag.py` 文件头 docstring（第 1~20 行）就是作者写好的"一页纸总结"。
3. **只精读一个函数**：`search()`——把 5.1→5.6 按代码顺序走一遍，开关全关时它就是"embed → 余弦 → top_k → 拼引用"，其余都是往上叠的可选层。
4. 有余量再看 `_rrf_fuse()` 和 `_rerank_hits()` 的注释，两个函数的 docstring 把"为什么"写得比代码还清楚。
