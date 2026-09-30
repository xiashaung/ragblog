"""嵌入分批的序号对齐回归测试（不联网，桩掉 SDK 客户端）。

看住两件事：

1. 网关回的 ``index`` 是**本批 input 内**的序号（每批从 0 重排），拼回时必须换算成
   全局位置。以前直接拿它当全局下标用，第 2 批会把第 1 批盖掉，尾部格子永远空着，
   最后 ``EmbeddingResponse`` 校验炸在 ``items.25``（= APP_EMBEDDING_MAX_BATCH）。
2. 向量与输入必须一一对应：网关乱序回也按 index 落位。
"""

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.schemas.ai import EmbeddingRequest, Provider
from app.services import llm

DIM = 4


def _patch_settings(monkeypatch: pytest.MonkeyPatch, *, batch: int) -> None:
    settings = Settings(_env_file=None, embedding_max_batch=batch, llm_retry_backoff_base=0.0)
    monkeypatch.setattr(llm, "get_settings", lambda: settings)


def _gateway(
    backend: llm.OpenAIBackend, *, shuffle: bool = False, global_index: bool = False
) -> list[int]:
    """假 ``/embeddings``：逐批回包，返回向量值 = 全局序号，并记录每批的输入条数。

    ``shuffle`` 模拟网关乱序回；``global_index`` 模拟少数网关直接回全局序号。
    """
    sizes: list[int] = []

    async def create(*, model: str, input: list[str]) -> SimpleNamespace:  # noqa: A002
        base = sum(sizes)
        sizes.append(len(input))
        # 每行 = (向量值 = 全局序号, 网关回的 index)。正常网关回批内序号 0..n-1；
        # global_index 那档模拟少数网关直接回全局序号。
        rows = [(base + i, base + i if global_index else i) for i in range(len(input))]
        if shuffle:
            rows = rows[::-1]
        data = [SimpleNamespace(index=idx, embedding=[float(g)] * DIM) for g, idx in rows]
        return SimpleNamespace(model=model, data=data, usage=None)

    backend._client = SimpleNamespace(embeddings=SimpleNamespace(create=create))
    return sizes


async def test_embed_batches_align_to_global_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """跨批次按输入顺序拼回：index 是全局下标，向量与输入一一对应，不留洞。"""
    _patch_settings(monkeypatch, batch=25)
    backend = llm.OpenAIBackend(Provider.OPENAI)
    sizes = _gateway(backend)
    items, _, _ = await backend.embed([f"t{i}" for i in range(60)])
    assert sizes == [25, 25, 10]  # 整批投递，不切得更碎
    assert [i.index for i in items] == list(range(60))
    assert [i.embedding[0] for i in items] == [float(i) for i in range(60)]


async def test_response_schema_accepts_multi_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """回归 ``items.25 Input should be ... EmbeddingItem``：响应体任何位置都不能是 None。"""
    _patch_settings(monkeypatch, batch=25)
    backend = llm.OpenAIBackend(Provider.OPENAI)
    _gateway(backend)
    resp = await backend.embeddings(
        EmbeddingRequest(provider=Provider.OPENAI, input=[f"t{i}" for i in range(26)])
    )
    assert [i.index for i in resp.items] == list(range(26))


async def test_embed_follows_gateway_indexing(monkeypatch: pytest.MonkeyPatch) -> None:
    """乱序回包按 index 落位；回全局序号的网关也认。"""
    _patch_settings(monkeypatch, batch=2)

    shuffled = llm.OpenAIBackend(Provider.OPENAI)
    _gateway(shuffled, shuffle=True)
    items, _, _ = await shuffled.embed(["a", "b", "c", "d"])
    assert [i.index for i in items] == list(range(4))
    assert [i.embedding[0] for i in items] == [0.0, 1.0, 2.0, 3.0]

    globalish = llm.OpenAIBackend(Provider.OPENAI)
    _gateway(globalish, global_index=True)
    items2, _, _ = await globalish.embed(["a", "b", "c", "d"])
    assert [i.index for i in items2] == list(range(4))
    assert [i.embedding[0] for i in items2] == [0.0, 1.0, 2.0, 3.0]


async def test_embed_raises_when_gateway_returns_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """网关少回条目时报错要说清「缺几条」，而不是把 None 漏进 pydantic 校验。"""
    _patch_settings(monkeypatch, batch=2)
    backend = llm.OpenAIBackend(Provider.OPENAI)

    async def create(*, model: str, input: list[str]) -> SimpleNamespace:  # noqa: A002
        return SimpleNamespace(
            model=model, data=[SimpleNamespace(index=0, embedding=[0.1] * DIM)], usage=None
        )

    backend._client = SimpleNamespace(embeddings=SimpleNamespace(create=create))
    # 两批各只回 1 条（批内 index 0）：落在 0 与 2 上，1、3 仍是洞。
    with pytest.raises(ValueError, match=r"缺 2/4 条（下标 \[1, 3\]"):
        await backend.embed(["a", "b", "c", "d"])


async def test_embed_images_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    """图片嵌入同样分批对齐（早先它拿批内序号往空 list 上做下标赋值，必然 IndexError）。"""
    _patch_settings(monkeypatch, batch=25)
    backend = llm.OpenAIBackend(Provider.OPENAI)
    sizes = _gateway(backend)
    items, _, _ = await backend.embed_images([f"img{i}" for i in range(30)])
    assert sizes == [25, 5]
    assert [i.index for i in items] == list(range(30))
    assert [i.embedding[0] for i in items] == [float(i) for i in range(30)]
