"""孤儿媒体文件回收测试：引用计数、删除路径与 sweep 宽限期。

全部走真实 API + tmp 媒体目录（monkeypatch settings.media_dir），断言直接
落在磁盘文件的存在性上——这个功能的效果本来就只在磁盘上可见。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import media_gc
from app.config import get_settings

PNG = b"\x89PNG\r\n\x1a\nfake"


@pytest.fixture()
def media_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """把媒体目录指到 tmp，返回 uploads 路径。"""
    uploads = tmp_path / "media" / "uploads"
    uploads.mkdir(parents=True)
    monkeypatch.setattr(get_settings(), "media_dir", str(tmp_path / "media"))
    return uploads


# ---------------- 单元测试 ----------------


def test_url_to_path_accepts_and_rejects(media_home: Path) -> None:
    ok = media_gc.url_to_path("/media/uploads/a.png")
    assert ok == media_home / "a.png"
    # 查询串/锚点剥掉后仍指同一文件
    assert media_gc.url_to_path("/media/uploads/a.png?v=2#x") == media_home / "a.png"
    # 外链、别的前缀、目录穿越一律不认
    assert media_gc.url_to_path("https://cdn.example.com/media/uploads/a.png") is None
    assert media_gc.url_to_path("/static/uploads/a.png") is None
    assert media_gc.url_to_path("/media/uploads/../secret.txt") is None
    assert media_gc.url_to_path("/media/uploads/") is None


def test_extract_markdown_images() -> None:
    md = "开头\n![alt](/media/uploads/a.png)\n中间\n![]( /media/uploads/b.png )\n"
    assert media_gc.extract_markdown_images(md) == ["/media/uploads/a.png"]
    assert media_gc.extract_markdown_images("") == []
    assert media_gc.extract_markdown_images(None) == []


# ---------------- API 级：删除路径的引用计数 ----------------


async def _upload_image(client, headers) -> str:
    resp = await client.post(
        "/api/v1/upload/image",
        files={"file": ("shot.png", PNG, "image/png")},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["url"]


async def _create_post(client, headers, title: str, content: str, cover: str | None = None):
    resp = await client.post(
        "/api/v1/admin/blogs",
        json={"title": title, "content": content, "cover_url": cover},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


async def test_delete_doc_releases_image(
    client, admin_headers, media_home, monkeypatch
) -> None:
    """图片入库建文档 → 删文档 → 磁盘文件被回收。"""
    from app.routers import upload as upload_mod

    async def fake_describe(data_url, provider):
        return "图表展示销售趋势，逐季度稳步上升。"

    monkeypatch.setattr(upload_mod, "_describe_image", fake_describe)
    resp = await client.post(
        "/api/v1/upload",
        files={"file": ("chart.png", PNG, "image/png")},
        headers=admin_headers,
    )
    data = resp.json()["data"]
    assert data["status"] == "success", data
    files = list(media_home.iterdir())
    assert len(files) == 1 and files[0].exists()

    resp = await client.delete(
        f"/api/v1/ai/knowledge/{data['knowledge_id']}", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert not files[0].exists()  # 文档删了，字节也跟删


async def test_shared_image_survives_until_last_reference(
    client, admin_headers, media_home
) -> None:
    """两篇文章引用同一张图：删一篇文件还在，删最后一片才回收。"""
    url = await _upload_image(client, admin_headers)
    path = media_home / url.split("/")[-1]
    p1 = await _create_post(client, admin_headers, "其一", f"正文一\n![图]({url})", url)
    p2 = await _create_post(client, admin_headers, "其二", f"正文二\n![图]({url})")

    resp = await client.delete(f"/api/v1/admin/blogs/{p1['id']}", headers=admin_headers)
    assert resp.status_code == 204
    assert path.exists()  # p2 还引用着

    resp = await client.delete(f"/api/v1/admin/blogs/{p2['id']}", headers=admin_headers)
    assert resp.status_code == 204
    assert not path.exists()


async def test_update_cover_releases_old_file(client, admin_headers, media_home) -> None:
    """换封面：旧文件无人引用被回收，新文件保留。"""
    old = await _upload_image(client, admin_headers)
    new = await _upload_image(client, admin_headers)
    old_path = media_home / old.split("/")[-1]
    new_path = media_home / new.split("/")[-1]
    post = await _create_post(client, admin_headers, "封面测试", "正文足够长的一段。", old)

    resp = await client.patch(
        f"/api/v1/admin/blogs/{post['id']}",
        json={"cover_url": new},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert not old_path.exists()
    assert new_path.exists()


async def test_deleted_body_image_released_keeps_still_cited(
    client, admin_headers, media_home
) -> None:
    """编辑正文删掉一张图：它不再被引用即回收；另一张仍写着就保留。"""
    drop = await _upload_image(client, admin_headers)
    keep = await _upload_image(client, admin_headers)
    drop_path = media_home / drop.split("/")[-1]
    keep_path = media_home / keep.split("/")[-1]
    post = await _create_post(
        client, admin_headers, "删图测试", f"正文\n![a]({drop})\n![b]({keep})"
    )

    resp = await client.patch(
        f"/api/v1/admin/blogs/{post['id']}",
        json={"content": f"改后正文，只留一张。\n![b]({keep})"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert not drop_path.exists()
    assert keep_path.exists()


# ---------------- API 级：sweep 兜底与宽限期 ----------------


async def test_sweep_grace_protects_fresh_upload(
    client, admin_headers, media_home
) -> None:
    """编辑器场景：图刚上传、正文还没保存（零引用），默认宽限期内不能扫掉。"""
    url = await _upload_image(client, admin_headers)
    path = media_home / url.split("/")[-1]

    resp = await client.post("/api/v1/admin/media/sweep", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    stats = resp.json()["data"]
    assert stats == {"scanned": 1, "deleted": 0, "kept": 1}
    assert path.exists()

    # 宽限期归零后（历史欠账清理场景）才真的删
    resp = await client.post(
        "/api/v1/admin/media/sweep", params={"grace_minutes": 0}, headers=admin_headers
    )
    stats = resp.json()["data"]
    assert stats["deleted"] == 1
    assert not path.exists()


async def test_sweep_keeps_referenced_files(client, admin_headers, media_home) -> None:
    url = await _upload_image(client, admin_headers)
    await _create_post(client, admin_headers, "sweep 引用", f"正文\n![图]({url})")
    # 造一个真孤儿：直接落盘不入库
    orphan = media_home / "orphan0123456789abcdef.png"
    orphan.write_bytes(PNG)
    import time

    past = time.time() - 7200
    import os

    os.utime(orphan, (past, past))

    resp = await client.post("/api/v1/admin/media/sweep", headers=admin_headers)
    stats = resp.json()["data"]
    assert stats["deleted"] == 1 and stats["kept"] == 1
    assert orphan.exists() is False
    assert (media_home / url.split("/")[-1]).exists()
