import numpy as np

# ============ A. 假的 embedding 函数（教学用）============
# 真实场景换成 sentence-transformers：
#   from sentence_transformers import SentenceTransformer
#   model = SentenceTransformer('paraphrase-MiniLM-L6-v2')
#   vecs = model.encode(texts, normalize_embeddings=True)
#
# 这里用"字袋"思路模拟：每个维度代表一个汉字/字符，
# 字面/语义相关的句子向量在空间里就会更近。
# 关键：用一个全局字表，保证所有向量维度一致。

def build_vocab(texts):
    chars = set()
    for t in texts:
        chars.update(list(t))
    return sorted(chars)


def embed(texts, vocab):
    """把文本列表变成 [N, dim] 矩阵，每行 L2 归一化（配合余弦相似度）。"""
    idx = {w: i for i, w in enumerate(vocab)}
    out = []
    for t in texts:
        vec = np.zeros(len(vocab))
        for ch in t:
            vec[idx[ch]] += 1.0
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        out.append(vec)
    return np.array(out)


def chunk_text(text, chunk_size=120, overlap=30):
    """把长文档切成有重叠的小块（防止把信息切断）。"""
    chunks, start = [], 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += chunk_size - overlap  # 重叠，保持衔接
        if start >= len(text):
            break
    return chunks


# ---------- 准备资料 ----------
docs = {
    "产品A": "我们的云存储服务每天备份数据，支持多区域容灾，可用性百分之九十九点九。",
    "产品B": "我们的关系型数据库支持 SQL 查询，提供事务保证和主从复制功能。",
    "产品C": "我们的客服机器人基于大语言模型，能 24 小时自动回答用户问题。",
}

# ---------- 离线处理：先建统一字表 -> 切块 -> 向量化 -> 存库 ----------
all_texts = [t for text in docs.values() for t in chunk_text(text)]
all_texts += ["你们的数据怎么备份的？"]  # 把常用 query 也纳入，保持维度一致
vocab = build_vocab(all_texts)                 # 一个字表用到底

records = []
for name, text in docs.items():
    for chunk in chunk_text(text):
        vec = embed([chunk], vocab)[0]
        records.append({"text": chunk, "doc": name, "vec": vec})

vectors = np.array([r["vec"] for r in records])
print(f"库里有 {len(records)} 个向量块，每个向量 {vectors.shape[1]} 维\n")


def retrieve(query, top_k=2):
    """问题向量化 -> 算余弦相似度 -> 取 Top-K。"""
    q_vec = embed([query], vocab)[0]
    sims = q_vec @ vectors.T          # 相似度（向量已归一化）
    idxs = np.argsort(sims)[::-1][:top_k]
    return [(records[i]["doc"], round(float(sims[i]), 3)) for i in idxs]


if __name__ == "__main__":
    query = "你们的数据怎么备份的？"
    print(f"问题：{query}")
    print("检索结果（按相似度排序）：")
    for doc, score in retrieve(query):
        print(f"  [{score}] {doc}")
