"""上传录入的「生产形态」测试：落盘原件 → 登记后台任务 → Worker 处理。

测试模式（``APP_TEST_MODE=true``）默认同步执行，方便断言即时结果；这些用例显式
关掉它，验证真正的异步路径：响应是 ``queued`` + ``task_id``，结果靠任务状态端点
轮询，原件读完即删，重启遗留的 pending 行能重新入队。
"""

from __future__ import annotations

import asyncio
import os
import time

from app import db
from app.config import get_settings
from app.models import RagTaskRow
from app.services import document_parser, file_ingest, rag_queue

_LONG_TEXT = ("后台队列录入的正文，内容足够长。" * 8).encode()


def _ok(resp) -> dict:
    body = resp.json()
    return body["data"] if isinstance(body, dict) and "data" in body else body


async def _await_task(client, headers, task_id: str, *, attempts: int = 50) -> dict:
    """轮询后台任务直到终态（Worker 处理需要时间，不能假设请求返回时已完成）。"""
    for _ in range(attempts):
        resp = await client.get(f"/api/v1/ai/knowledge/tasks/{task_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        body = _ok(resp)
        if body["status"] in {rag_queue.SUCCESS, rag_queue.FAILED}:
            return body
        await asyncio.sleep(0.2)
    raise AssertionError(f"任务 {task_id} 未进入终态")


async def test_upload_is_queued_and_finished_by_worker(
    client, admin_headers, tmp_path, monkeypatch
) -> None:
    """生产形态的单文件上传：立即返 queued，后台入库，原件被删。"""
    monkeypatch.setenv("APP_TEST_MODE", "false")
    monkeypatch.setattr(get_settings(), "media_dir", str(tmp_path / "media"))

    resp = await client.post(
        "/api/v1/upload",
        files={"file": ("队列文档.txt", _LONG_TEXT, "text/plain")},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    out = _ok(resp)
    assert out["status"] == "queued"
    assert out["task_id"]
    assert out["knowledge_id"] is None

    uploads = tmp_path / "media" / "uploads"
    assert list(uploads.iterdir()), "原件应先落盘，等 Worker 读取"

    task = await _await_task(client, admin_headers, out["task_id"])
    assert task["status"] == rag_queue.SUCCESS, task["error"]
    assert task["result"]["chunks"] > 0
    assert task["result"]["filename"] == "队列文档.txt"

    listed = _ok(
        await client.get("/api/v1/ai/knowledge", params={"limit": 50}, headers=admin_headers)
    )["items"]
    row = next(i for i in listed if i["title"] == "队列文档")
    assert row["kind"] == "file"
    assert row["source_file"] == "队列文档.txt"

    # 原件不属于任何文档，回收器查不到引用：必须由 Worker 自己删掉
    assert not list(uploads.iterdir())


async def test_batch_upload_counts_queued_files(
    client, admin_headers, tmp_path, monkeypatch
) -> None:
    """批量上传走队列时，queued 与 failed 分开计数，坏文件仍单独报错。"""
    monkeypatch.setenv("APP_TEST_MODE", "false")
    monkeypatch.setattr(get_settings(), "media_dir", str(tmp_path / "media"))

    resp = await client.post(
        "/api/v1/upload/batch",
        files=[
            ("files", ("批量一.txt", ("批量录入的第一个文件。" * 6).encode(), "text/plain")),
            ("files", ("批量二.txt", ("批量录入的第二个文件。" * 6).encode(), "text/plain")),
            ("files", ("坏文件.exe", b"MZ\x00\x01", "application/octet-stream")),
        ],
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    body = _ok(resp)
    assert (body["total"], body["queued"], body["succeeded"], body["failed"]) == (3, 2, 0, 1)

    bad = next(r for r in body["results"] if r["status"] == "error")
    assert "不支持" in bad["error"]
    assert bad["task_id"] is None, "不支持的扩展名不该落盘、更不该登记任务"

    for item in body["results"]:
        if item["status"] == "queued":
            task = await _await_task(client, admin_headers, item["task_id"])
            assert task["status"] == rag_queue.SUCCESS, task["error"]

    titles = {
        i["title"]
        for i in _ok(
            await client.get("/api/v1/ai/knowledge", params={"limit": 50}, headers=admin_headers)
        )["items"]
    }
    assert {"批量一", "批量二"} <= titles
    assert not list((tmp_path / "media" / "uploads").iterdir())


async def test_same_title_from_different_files_does_not_overwrite(
    client, admin_headers
) -> None:
    """同名不同来源文件是两篇文档；重传同一个文件仍是替换。"""
    texts = {
        "notes.txt": ("来自 txt 的 notes，正文足够长。" * 6).encode(),
        "notes.md": ("来自 markdown 的 notes，正文也足够长。" * 6).encode(),
    }
    for name, raw in texts.items():
        resp = await client.post(
            "/api/v1/upload",
            files={"file": (name, raw, "text/plain")},
            headers=admin_headers,
        )
        assert _ok(resp)["status"] == "success", _ok(resp)["error"]

    items = _ok(
        await client.get("/api/v1/ai/knowledge", params={"limit": 50}, headers=admin_headers)
    )["items"]
    notes = [i for i in items if i["title"] == "notes"]
    assert len(notes) == 2
    assert {i["source_file"] for i in notes} == {"notes.txt", "notes.md"}

    # 同一文件重传：只更新那一篇，总数不变
    resp = await client.post(
        "/api/v1/upload",
        files={"file": ("notes.txt", ("来自 txt 的 notes，改过内容。" * 6).encode(), "text/plain")},
        headers=admin_headers,
    )
    updated_id = _ok(resp)["knowledge_id"]
    items = _ok(
        await client.get("/api/v1/ai/knowledge", params={"limit": 50}, headers=admin_headers)
    )["items"]
    assert len([i for i in items if i["title"] == "notes"]) == 2
    assert updated_id in {i["knowledge_id"] for i in items}


async def test_gbk_text_is_decoded_instead_of_garbled(client, admin_headers) -> None:
    """中文 TXT 常是 GBK 存的：解码失败会让整篇变成乱码，检索完全哑火。"""
    text = "这是一份用 GBK 编码保存的中文文档，正文足够长。" * 6
    resp = await client.post(
        "/api/v1/upload",
        files={"file": ("GBK 文档.txt", text.encode("gb18030"), "text/plain")},
        headers=admin_headers,
    )
    out = _ok(resp)
    assert out["status"] == "success", out["error"]

    detail = _ok(
        await client.get(f"/api/v1/ai/knowledge/{out['knowledge_id']}", headers=admin_headers)
    )
    assert "GBK 编码" in detail["content"]
    assert "\ufffd" not in detail["content"]


async def test_html_uploads_are_supported_and_stripped(client, admin_headers) -> None:
    """HTML 是允许的扩展名，标签压成正文后入库。"""
    html = (
        "<html><body><h2>部署手册</h2><p>这里写的是真正的正文内容，够长。</p>"
        "<style>.x{color:red}</style></body></html>"
    ).encode()
    resp = await client.post(
        "/api/v1/upload",
        files={"file": ("手册.html", html, "text/html")},
        headers=admin_headers,
    )
    out = _ok(resp)
    assert out["status"] == "success", out["error"]
    detail = _ok(
        await client.get(f"/api/v1/ai/knowledge/{out['knowledge_id']}", headers=admin_headers)
    )
    assert "真正的正文内容" in detail["content"]
    assert "color:red" not in detail["content"]


async def test_embedded_images_stop_at_attempt_cap(monkeypatch) -> None:
    """失败的图片描述也算尝试次数：否则一堆坏图 = 一堆白付的模型调用。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "max_embedded_images", 10)
    monkeypatch.setattr(settings, "max_image_description_attempts", 2)

    calls = 0

    async def fake_describe(data_url: str, provider: str | None = None) -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("视觉模型不可用")

    monkeypatch.setattr(file_ingest, "describe_image", fake_describe)

    images = [
        document_parser.ExtractedImage(data=b"png-bytes", ext="png", width=200, height=200)
        for _ in range(10)
    ]
    descriptions, urls, stats = await file_ingest.describe_embedded_images(images)
    assert calls == 2
    assert stats["attempts"] == 2 and stats["failed"] == 2
    assert descriptions == [] and urls == []


async def test_ingest_rejects_documents_over_char_cap(client, admin_headers, monkeypatch) -> None:
    """正文字符上限：超大文档分块+嵌入的成本不可控，要在入库前拦住。"""
    monkeypatch.setattr(get_settings(), "max_ingest_chars", 300)
    resp = await client.post(
        "/api/v1/upload",
        files={"file": ("超长.txt", ("正文太长了必须被拦住。" * 40).encode(), "text/plain")},
        headers=admin_headers,
    )
    out = _ok(resp)
    assert out["status"] == "error"
    assert "正文过长" in out["error"]


async def test_orphan_pending_tasks_are_requeued(client, admin_headers) -> None:
    """进程重启丢掉内存队列：库里 pending 的行重新入队，不会永远卡住。"""
    queue = rag_queue.get_queue()
    async with db.session_factory() as session:
        row = RagTaskRow(
            task_type=rag_queue.TaskType.INGEST.value,
            payload={"title": "遗留任务", "content": "重启前登记但没有执行的任务正文。" * 6},
            status=rag_queue.PENDING,
            created_at=time.time(),
        )
        session.add(row)
        await session.commit()
        task_id = row.id

    await queue._recover_orphans()
    task = await _await_task(client, admin_headers, task_id)
    assert task["status"] == rag_queue.SUCCESS, task["error"]


async def test_orphan_requeue_can_be_disabled(client, admin_headers, monkeypatch) -> None:
    """关掉自动重跑时只告警，不静默重复执行历史任务。"""
    monkeypatch.setattr(get_settings(), "rag_queue_requeue_orphans", False)
    queue = rag_queue.get_queue()
    async with db.session_factory() as session:
        row = RagTaskRow(
            task_type=rag_queue.TaskType.INGEST.value,
            payload={"title": "不该自动跑", "content": "管理员想自己确认后再重跑。" * 6},
            status=rag_queue.PENDING,
            created_at=time.time(),
        )
        session.add(row)
        await session.commit()
        task_id = row.id

    await queue._recover_orphans()
    assert queue._queue.empty()
    status = _ok(await client.get(f"/api/v1/ai/knowledge/tasks/{task_id}", headers=admin_headers))
    assert status["status"] == rag_queue.PENDING


async def test_sweep_keeps_files_waiting_for_pending_tasks(
    client, admin_headers, tmp_path, monkeypatch
) -> None:
    """等待后台任务处理的原件不属于任何文档，回收器不能把它当孤儿删掉。"""
    monkeypatch.setenv("APP_TEST_MODE", "false")
    monkeypatch.setattr(get_settings(), "media_dir", str(tmp_path / "media"))
    uploads = tmp_path / "media" / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)

    name = "pending-a1b2c3d4.txt"
    path = uploads / name
    path.write_bytes("排队中的上传原件".encode())
    past = time.time() - 7200
    os.utime(path, (past, past))

    async with db.session_factory() as session:
        row = RagTaskRow(
            task_type=rag_queue.TaskType.UPLOAD.value,
            payload={"media_path": f"uploads/{name}", "filename": "等待.txt"},
            status=rag_queue.PENDING,
            created_at=time.time(),
        )
        session.add(row)
        await session.commit()
        task_id = row.id

    stats = _ok(
        await client.post(
            "/api/v1/admin/media/sweep", params={"grace_minutes": 0}, headers=admin_headers
        )
    )
    assert stats["kept"] == 1 and stats["deleted"] == 0
    assert path.exists()


    # 任务不再 pending/running 之后，同一文件就是真孤儿，回收器应当清掉
    async with db.session_factory() as session:
        existing = await session.get(RagTaskRow, task_id)
        existing.status = rag_queue.SUCCESS
        await session.commit()

    stats = _ok(
        await client.post(
            "/api/v1/admin/media/sweep", params={"grace_minutes": 0}, headers=admin_headers
        )
    )
    assert stats["deleted"] == 1
    assert not path.exists()

async def test_text_and_pairs_ingest_are_queued(client, admin_headers, monkeypatch) -> None:
    """文本 / 问答对录入在生产模式同样走队列：响应是 pending + task_id，
    终态靠任务端点轮询 —— 前端不能把「0 个分片」当成成功。"""
    monkeypatch.setenv("APP_TEST_MODE", "false")

    resp = await client.post(
        "/api/v1/ai/knowledge/ingest",
        json={
            "title": "队列文本",
            "content": _LONG_TEXT.decode(),
            "format": "markdown",
            "chunk_size": 400,
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    out = _ok(resp)
    assert out["status"] == rag_queue.PENDING
    assert out["task_id"]
    assert out["knowledge_id"] == f"pending-{out['task_id']}"
    assert out["chunks"] == 0

    task = await _await_task(client, admin_headers, out["task_id"])
    assert task["status"] == rag_queue.SUCCESS, task["error"]
    assert task["result"]["chunks"] > 0

    resp = await client.post(
        "/api/v1/ai/knowledge/ingest-pairs",
        json={
            "title": "队列问答对",
            "pairs": [{"question": "怎么重启服务？", "answer": "运行 run.py 即可。"}],
            "chunk_size": 800,
        },
        headers=admin_headers,
    )
    out = _ok(resp)
    assert out["status"] == rag_queue.PENDING and out["task_id"]
    task = await _await_task(client, admin_headers, out["task_id"])
    assert task["status"] == rag_queue.SUCCESS, task["error"]

    docs = _ok(
        await client.get("/api/v1/ai/knowledge", params={"limit": 50}, headers=admin_headers)
    )["items"]
    titles = {d["title"]: d["status"] for d in docs}
    assert titles.get("队列文本") == "indexed"
    assert titles.get("队列问答对") == "indexed"

async def test_legacy_ingest_text_endpoint_also_queues(client, admin_headers, monkeypatch) -> None:
    """旧版纯文本入口不能绕过队列：生产模式同样返回 pending + task_id。"""
    monkeypatch.setenv("APP_TEST_MODE", "false")
    resp = await client.post(
        "/api/v1/ingest-text",
        data={"title": "旧版文本入口", "content": _LONG_TEXT.decode(), "chunk_size": 400},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    out = _ok(resp)
    assert out["status"] == rag_queue.PENDING and out["task_id"]

    task = await _await_task(client, admin_headers, out["task_id"])
    assert task["status"] == rag_queue.SUCCESS, task["error"]
    assert task["result"]["chunks"] > 0
