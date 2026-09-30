# 给小白讲清楚 RAG 检索：设计思路 + 流程 + 代码

> 目标：零基础也能听懂。从"为什么要 RAG"开始，一路讲到"代码怎么跑"，最后落到几个真实设计决策。

---

## 0. 一句话理解 RAG

**RAG = Retrieval-Augmented Generation（检索增强生成）。**

它解决一个核心矛盾：

> 大模型（LLM）知道"通识"，但不知道"你的私有数据"，而且会**幻觉**（瞎编）。

RAG 的做法是——**在回答之前，先去你的资料库里把相关的东西搜出来，塞给模型当"参考资料"，让它根据资料答。**

打个比方：
- 没有 RAG：闭卷考试，考不会的就是不会，还会瞎编。
- 有 RAG：开卷考试，先翻书（检索）再答题（生成）。

---

## 1. 整体流程（记住这 4 步）

```
┌─────────────────────────────────────────────────────────┐
│                    离线阶段（一次性/定期跑）                │
│                                                           │
│  ① 文档  →  ② 切块(chunk)  →  ③ 向量化(embedding)  →  存 │
│     (pdf/txt/md)    (切段落)        (数字向量)      库     │
│                                                  ┌───────┤
│                                                  │ 向量库 │
│                                                  │(FAISS) │
└───────────────────────────────────────────────────┴───────┘
                          ▲
                          │
┌─────────────────────────────────────────────────────────┐
│                    在线阶段（用户提问时实时跑）             │
│                                                           │
│  ④ 用户问题  →  ⑤ 问题向量化  →  ⑥ 向量库检索 Top-K 相似   │
│      (text)        (embedding)      文档块                  │
│                                                        │
│  ⑦ 把检索到的块 + 问题 拼成 prompt  →  ⑧ LLM 生成答案      │
└─────────────────────────────────────────────────────────┘
```

**一句话串联：**
> 先把资料"嚼碎→变成向量存起来"（离线）；用户一问，"把问题也变成向量→去库里找最相似的几块→拼给模型→让它抄答案"（在线）。

---

## 2. 核心概念逐个击破

### 2.1 什么是 Embedding（向量化）？

把一段文字变成一个**数字数组**（向量），**语义相近的文本，向量在空间里也靠得很近**。

- "猫在沙发上睡觉" → `[0.12, -0.87, 0.45, ...]`
- "小狗趴在床上休息" → `[0.15, -0.80, 0.41, ...]`
- "量子力学波函数" → `[0.93, 0.22, -0.67, ...]`

前两段距离近，第三段距离远。机器就是靠这个判断"相关"。

**怎么算相近？** 最常用 **余弦相似度（Cosine Similarity）**，范围 [-1, 1]，越接近 1 越相似。

### 2.2 什么是 Chunk（切块）？

一篇文档太长，不能整体向量化（太长会被截断、也搜不细），所以要**切成小段落**。

- 切太大：粒度粗，搜出来的东西不精准。
- 切太小：缺上下文，语义不完整。
- 常见做法：按字符数切（比如 512 字符）+ 重叠（overlap 比如 50 字符），保证段落衔接。

### 2.3 什么是向量库（Vector Store）？

专门存向量、并能快速找"最近邻"的数据库。
- 教学用：**FAISS**（Facebook 出品，纯本地、快、免安装服务）
- 生产用：Milvus、Qdrant、Pinecone、Weaviate 等

**为什么不能 brute force 暴力算？** 数据量一大（百万级），两两算距离会超时。向量库用 **ANN（近似最近邻）算法**（HNSW、IVF 等）换一点点精度换来巨大速度。

---

## 3. 完整可跑代码（最小示例）

下面这套代码**不依赖任何框架**，只用 `numpy` + 一个假的 embedding 函数，让你看清每一步在干什么。跑通后再去看 LangChain/LlamaIndex 才是真懂。

> 真正的 embedding 换成 `sentence-transformers` 或 OpenAI embedding 即可，流程完全一样。

### 3.0 安装

```bash
pip install numpy
# 真正用：pip install sentence-transformers faiss-cpu
```

### 3.1 第一步：把文档切块 + 向量化 + 入库（离线）

```python
import numpy as np

# ============ A. 一个假的 embedding 函数（教学用）============
# 真实场景换成 sentence-transformers：
#   from sentence_transformers import SentenceTransformer
#   model = SentenceTransformer('paraphrase-MiniLM-L6-v2')
#   def embed(texts): return model.encode(texts, normalize_embeddings=True)
#
# 这里用一个"字袋"思路模拟：每个维度代表一个字符，
# 字面/语义相关的句子向量在空间里就更近。
# 关键：用一个统一字表，保证所有向量维度一致（文档块和问题共用一张表）。

def build_vocab(texts):
    """收集所有字符，生成统一字表（返回排序后的字符列表）。"""
    chars = set()
    for t in texts:
        chars.update(list(t))
    return sorted(chars)


def embed(texts, vocab):
    """把文本列表变成 [N, dim] 的向量矩阵，每行 L2 归一化（配合余弦相似度）。"""
    word_idx = {w: i for i, w in enumerate(vocab)}  # 字 -> 索引
    mats = []
    for t in texts:
        vec = np.zeros(len(vocab))
        for ch in t:
            vec[word_idx[ch]] += 1.0
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        mats.append(vec)
    return np.array(mats)


def chunk_text(text, chunk_size=120, overlap=30):
    """把长文档切成有重叠的小块。"""
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += (chunk_size - overlap)  # 重叠，保持衔接
        if start >= len(text):
            break
    return chunks


# ---------- 准备资料 ----------
docs = {
    "产品A": "我们的云存储服务每天备份数据，支持多区域容灾，可用性百分之九十九点九。",
    "产品B": "我们的关系型数据库支持 SQL 查询，提供事务保证和主从复制功能。",
    "产品C": "我们的客服机器人基于大语言模型，能 24 小时自动回答用户问题。",
}

# ---------- 离线处理：切块 + 向量化 + 存库 ----------
# 关键：先建一张统一字表，所有块和问题共用它，维度才一致。
all_texts = [text for text in docs.values() for text in chunk_text(text)]
all_texts.append("你们的数据怎么备份的？")  # 把常用问题也纳入
vocab = build_vocab(all_texts)               # 一个字表用到底

records = []          # 存放 (原始块文本, 来源文档, 向量)
for name, text in docs.items():
    for chunk in chunk_text(text):
        vec = embed([chunk], vocab)[0]
        records.append({"text": chunk, "doc": name, "vec": vec})

vectors = np.array([r["vec"] for r in records])
print(f"库里有 {len(records)} 个向量块，每个向量 {vectors.shape[1]} 维\n")
```

### 3.2 第二步：用户提问 → 检索（在线）

```python
def cosine_similarity(a, b):
    """a, b 都是 L2 归一化后的向量，余弦相似度就是点积。"""
    return a @ b.T


def retrieve(query, top_k=2):
    """把问题向量化，然后在库里找最相似的前 top_k 块。"""
    q_vec = embed([query])[0].reshape(1, -1)      # 问题向量化
    sims = cosine_similarity(q_vec, vectors)        # 算相似度
    # 取分数最高的 top_k 个索引
    idxs = np.argsort(sims[0])[::-1][:top_k]
    return [(records[i]["text"], records[i]["doc"], float(sims[0][i])) for i in idxs]


query = "你们的数据怎么备份的？"
hits = retrieve(query)
print(f"问题：{query}\n检索结果：")
for text, doc, score in hits:
    print(f"  [{score:.3f}] ({doc}) {text}")
```

### 3.3 第三步：拼 Prompt + 让模型生成（合成）

```python
def build_prompt(query, hits):
    context = "\n".join(f"- {h[0]}" for h in hits)
    return f"""请仅根据下面的参考资料回答问题。如果资料里没有答案，就说不知道。

参考资料：
{context}

问题：{query}

回答："""


prompt = build_prompt("你们的数据怎么备份的？", retrieve("你们的数据怎么备份的？"))
print("\n---- 送给 LLM 的 Prompt ----")
print(prompt)

# 真实调用示例（OpenAI）：
# from openai import OpenAI
# client = OpenAI()
# resp = client.chat.completions.create(
#     model="gpt-4o-mini",
#     messages=[{"role": "user", "content": prompt}],
# )
# print(resp.choices[0].message.content)
```

### 3.4 接上真正的 FAISS 向量库（生产必备）

上面是裸 numpy 演示。数据量大时必须用 FAISS 的 ANN：

```python
# pip install faiss-cpu
import faiss

dim = vectors.shape[1]
index = faiss.IndexFlatL2(dim)   # 精确搜索；百万级改用 faiss.IndexHNSWFlat
index.add(vectors)               # 入库
k = 2
dist, idx = index.search(vectors[:1], k)   # query, 返回距离和索引
```

> 关键点：`IndexFlatL2` 是暴力搜索，数据一上百万就换成 `IndexHNSWFlat`（近似最近邻），流程代码**完全不用改**，只换这一行。

---

## 4. 关键设计决策（面试/实战高频考点）

### 4.1 切块策略（Chunking）
- **固定长度切分**：简单，但可能切断句子。
- **按语义切分**：按段落、标题、AST（代码）切，质量高。
- **重叠（Overlap）**：防止关键信息被切在两块中间。
- **父子块 / 元数据过滤**：大块做精排，小块做召回；按文档类型/时间/作者过滤。

### 4.2 检索增强（不止算一次相似度）
- **混合检索（Hybrid Search）**：向量检索（语义）+ 关键词检索（BM25，精确匹配术语），两者结果融合。很多场景比纯向量好很多。
- **重排序（Rerank）**：先召回 Top-50，再用 Cross-Encoder 精排到 Top-5，牺牲速度换精度。
- **Query 改写**：把用户问题扩展/拆分成更适合检索的形式（Query Expansion）。

### 4.3 评估（怎么知道检索好不好？）
- **召回率 Recall@K**：相关文档有没有出现在前 K 个里。
- **MRR / nDCG**：排在第几位。
- 有标注数据时算精确指标；没标注就看人工抽查。

---

## 5. 常见误区（小白最容易踩的坑）

| 误区 | 真相 |
|------|------|
| "向量检索一定比关键词强" | 术语/代码/专有名词，BM25 关键词检索往往更强。要用**混合检索**。 |
| "Embedding 越新越好" | 要匹配你的领域。医疗/法律场景用领域模型更好。 |
| "Chunk 越大越好" | 太大粒度粗、噪音多；太小丢上下文。要找平衡。 |
| "检索完直接喂给模型就完事" | 通常还需要 Rerank 和 Prompt 工程，效果才好。 |
| "RAG 能解决所有幻觉" | RAG 主要解决"不知道私有知识"的问题；模型本身的推理幻觉它管不了。 |

---

## 6. 进阶路线（学完上面这些之后）

1. **LangChain / LlamaIndex**：把上面这些步骤封装成框架，快速搭流水线。
2. **多模态 RAG**：检索图片、表格、PDF 里的内容。
3. **Agentic RAG**：检索不到就主动多查几次，或调用工具。
4. **GraphRAG**：用知识图谱增强，处理"实体关系"类问题。

---

## 核心记忆点（考前背这三句）

1. **RAG = 先查资料再答题**，解决私有知识 + 幻觉。
2. **流程**：文档→切块→向量化→存库（离线）；提问→向量化→检索Top-K→拼Prompt→生成（在线）。
3. **真相**：纯向量检索不够，实战要**混合检索 + 重排序**。
