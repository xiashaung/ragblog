"""上传大小闸门测试：_read_capped 单元行为 + 三个入口的超限表现。"""

from __future__ import annotations

import io

from starlette.datastructures import UploadFile

from app.routers.upload import (
    MAX_DOCUMENT_BYTES,
    MAX_IMAGE_BYTES,
    _read_capped,
)


def _upload(payload: bytes, filename: str) -> UploadFile:
    return UploadFile(filename=filename, file=io.BytesIO(payload))


async def test_read_capped_returns_within_limit() -> None:
    raw = await _read_capped(_upload(b"hello" * 100, "a.txt"), 1024 * 1024)
    assert raw.startswith(b"hello")


async def test_read_capped_aborts_over_limit() -> None:
    # 9 MB 出头的体、8 MB 的限：必须拒掉；文案带 MB 上限便于前端直接展示
    payload = b"x" * (8 * 1024 * 1024 + 1024)
    try:
        await _read_capped(_upload(payload, "big.pdf"), 8 * 1024 * 1024)
    except ValueError as exc:
        assert "过大" in str(exc)
        assert "8 MB" in str(exc)
    else:
        raise AssertionError("超限文件必须被拒")


async def test_upload_rejects_oversized_document(client, console) -> None:
    """单文件入口：超限不炸内存，按既有的 200 + status=error 语义返回。"""
    big = b"x" * (MAX_DOCUMENT_BYTES + 1024)
    res = await client.post(
        "/api/v1/upload",
        files={"file": ("big.txt", big, "text/plain")},
        headers=console["headers"],
    )
    assert res.status_code == 200
    assert "过大" in res.text


async def test_batch_isolates_oversized_file(client, console) -> None:
    """批量入口：坏文件只记自己那条 error，好文件照常入库。"""
    ok = "这是一份足够长的正常文本文件。" * 20
    big = b"x" * (MAX_DOCUMENT_BYTES + 1024)
    res = await client.post(
        "/api/v1/upload/batch",
        files=[
            ("files", ("small.txt", ok.encode(), "text/plain")),
            ("files", ("huge.txt", big, "text/plain")),
        ],
        headers=console["headers"],
    )
    assert res.status_code == 200
    body = res.json()["data"]
    assert body["succeeded"] == 1 and body["failed"] == 1
    err = next(r for r in body["results"] if r["status"] == "error")
    assert "过大" in err["error"]


async def test_upload_image_rejects_oversized(client, console) -> None:
    """图片入口走全局 ValueError→400 出口，文案含上限。"""
    big = b"x" * (MAX_IMAGE_BYTES + 1024)
    res = await client.post(
        "/api/v1/upload/image",
        files={"file": ("big.png", big, "image/png")},
        headers=console["headers"],
    )
    assert res.status_code == 400
    assert "过大" in res.text
