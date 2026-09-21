"""文件上传与知识录入路由（RAG 的写入口）。

一个接口跑完全链路：收 multi-part 文件 → 解析（PDF/DOCX/TXT/MD/CSV/LOG/图片）
→ 分块 → 向量化 → 索引入库。图片没有文字层，先让视觉模型写成中文描述再入库。

三个入口共用 :func:`_parse_and_ingest` 这一条代码路径：单文件、批量（每文件一个
savepoint，坏文件不拖累好文件）、以及旧版纯文本录入。
"""

from __future__ import annotations

import base64
import io
import logging
import pathlib
import time
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from .. import services
from ..config import get_settings
from ..db import get_db
from ..schemas.ai import ChatMessage
from ..schemas.upload import (
    KnowledgeIngestRequest,
    KnowledgeIngestResponse,
    UploadBatchItem,
    UploadBatchResponse,
    UploadStatusResponse,
)
from ..services.llm import get_llm

logger = logging.getLogger(__name__)
router = APIRouter()

# 允许录入的图片格式。纯文本嵌入模型没法把图片向量化，所以录入时改由支持视觉的
# 对话模型写一段中文描述当可检索正文；原图落盘到媒体目录，只把 URL 存进
# ``metadata["images"]``，浏览器能直接看，数据库不埋 base64 大 blob。
IMAGE_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
}
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 单图 5 MB，兼作 meta 里 JSON 体积的上限
# 文档内嵌图（PDF/DOCX 里的插图）要一张张调视觉模型，所以限张数、并跳过装饰图
MAX_EMBEDDED_IMAGES = 6  # 每篇文档最多描述 6 张内嵌图
MIN_EMBEDDED_PIXELS = 6_400  # 跳过小图标/小 logo（比如 80x80）

_IMAGE_PROMPT = (
    "请用中文详细描述这张图片的内容，包括主体、文字、图表数据与场景，"
    "以便后续据此检索与问答。只输出描述文本，不要额外解释。"
)


def _image_data_url(raw: bytes, filename: str) -> str:
    """按文件扩展名猜 MIME，把字节包成内联 data URL（只用来喂模型）。"""
    ext = pathlib.Path(filename).suffix.lstrip(".").lower()
    mime = IMAGE_MIME.get(ext, "image/png")
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _bytes_data_url(data: bytes, ext: str) -> str:
    """给已经解成字节的内嵌图拼 data URL（``ext`` 形如 ``'png'``）。"""
    mime = IMAGE_MIME.get(ext, "image/png")
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _save_image_file(data: bytes, ext: str) -> str:
    """图片字节落盘到媒体目录，返一个浏览器能直接访问的 URL。

    字节只进磁盘不进库：``metadata["images"]`` 存的是返回的
    ``/media/uploads/<uuid>.<ext>`` 路径，SPA 拿去直接展示。喂视觉模型时仍然用
    手上字节现拼的内联 data URL，所以访问不到本服务 URL 的本地模型也照样能用。
    """
    settings = get_settings()
    safe_ext = ext if ext in IMAGE_MIME else "png"
    uploads = settings.media_root / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}.{safe_ext}"
    (uploads / name).write_bytes(data)
    return f"{settings.media_url.rstrip('/')}/uploads/{name}"


async def _describe_embedded_images(
    images: list, provider: str | None
) -> tuple[list[str], list[str]]:
    """用视觉模型描述文档内嵌图，返 ``(描述列表, 图片URL列表)``。

    ``图片URL`` 都是磁盘上的媒体地址（不是 blob）。过小 / 装饰图直接跳过，单张失败
    也只跳不抛（一张坏图不能拖垮整个上传）；描述次数与落盘次数都封顶，
    图多的文档不至于把入库时间拉成不可控。
    """
    descriptions: list[str] = []
    urls: list[str] = []
    described = 0
    for idx, img in enumerate(images, 1):
        if described >= MAX_EMBEDDED_IMAGES:
            break
        if img.pixels and img.pixels < MIN_EMBEDDED_PIXELS:
            continue
        if not img.data or len(img.data) > MAX_IMAGE_BYTES:
            continue
        data_url = _bytes_data_url(img.data, img.ext)  # 仅用于喂视觉模型
        try:
            desc = await _describe_image(data_url, provider)
        except Exception as exc:  # noqa: BLE001 - 这张跳过，继续后面的
            logger.warning("描述内嵌图片失败（已跳过第 %s 张）：%s", idx, exc)
            continue
        described += 1
        if desc and len(desc.strip()) >= 5:
            descriptions.append(f"[图片{idx}] {desc.strip()}")
            if len(urls) < MAX_EMBEDDED_IMAGES:
                try:
                    urls.append(_save_image_file(img.data, img.ext))
                except OSError as exc:  # 落盘失败不影响文字入库
                    logger.warning("保存内嵌图片失败（第 %s 张）：%s", idx, exc)
    return descriptions, urls


async def _describe_image(data_url: str, provider: str | None) -> str:
    """让配置里的（视觉）对话模型描述一张图，返文字。

    图片描述是「对话」能力，用系统配置里的 chat_provider（不是 embedding_provider）；
    请求显式传了 provider 则以请求为准。
    """
    backend = get_llm(provider, feature="chat")
    content, _finish, _usage, _model = await backend.chat(
        [
            ChatMessage(
                role="user",
                content=[
                    {"type": "text", "text": _IMAGE_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            )
        ]
    )
    return (content or "").strip()


async def _parse_and_ingest(
    session: AsyncSession,
    *,
    raw: bytes,
    filename: str,
    title: str | None,
    provider: str | None,
    chunk_size: int | None,
) -> tuple[str, int, int]:
    """解析一个上传件并入库；失败抛 ``ValueError``（中文描述）。

    返 ``(knowledge_id, 分块数, 字数)``。里层不碰任何 HTTP 概念，单文件与批量
    两个入口才能共用这一条路径。
    """
    if not raw:
        raise ValueError("文件内容为空")

    ext = pathlib.Path(filename).suffix.lstrip(".").lower()
    doc_title = (title or pathlib.Path(filename).stem or "Unknown Document").strip()[:200]

    # --- 图片：用视觉模型转描述，字节留下来供展示 ---
    if ext in IMAGE_MIME:
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError(
                f"图片过大（{len(raw) // 1024 // 1024} MB），"
                f"单张上限 {MAX_IMAGE_BYTES // 1024 // 1024} MB"
            )
        data_url = _image_data_url(raw, filename)  # 仅用于喂视觉模型
        try:
            description = await _describe_image(data_url, provider)
        except Exception as exc:  # noqa: BLE001 - 模型/格式错误按单文件上报
            raise ValueError(f"图片识别失败（需配置支持视觉的对话模型）：{exc}") from exc
        if not description or len(description.strip()) < 10:
            raise ValueError("视觉模型未生成有效图片描述，请确认后端配置的是支持视觉的对话模型")
        image_url = _save_image_file(raw, ext)
        ingest_req = KnowledgeIngestRequest(
            title=doc_title,
            content=description,
            provider=services.rag._resolved_provider(provider),
            chunk_size=chunk_size,
            metadata={
                "kind": "image",
                "source_file": filename,
                "images": [image_url],
                "image_storage": "file",
                "uploaded_at": time.time(),
            },
        )
        result = await services.rag.ingest(session, ingest_req)
        return result.knowledge_id, result.chunks, result.chars

    # --- 含文本的文档（包括 PDF / DOCX 内嵌的图）---
    parsed = await services.document_parser.parse_document_full(io.BytesIO(raw), filename)
    text = (parsed.text or "").strip()

    image_urls: list[str] = []
    descriptions: list[str] = []
    if parsed.images:
        descriptions, image_urls = await _describe_embedded_images(parsed.images, provider)
    if descriptions:
        text = f"{text}\n\n" + "\n".join(descriptions) if text else "\n".join(descriptions)
        text = text.strip()

    if not text or len(text) < 10:
        raise ValueError(
            f"提取文本过短（{len(text)} 字），可能是扫描版 PDF 或格式不支持"
        )

    metadata: dict = {
        "kind": "file",
        "source_file": filename,
        "uploaded_at": time.time(),
    }
    if parsed.images:
        metadata["embedded_images"] = len(parsed.images)
    if descriptions:
        metadata["described_images"] = len(descriptions)
    if image_urls:
        metadata["images"] = image_urls

    ingest_req = KnowledgeIngestRequest(
        title=doc_title,
        content=text,
        provider=services.rag._resolved_provider(provider),
        chunk_size=chunk_size,
        metadata=metadata,
    )
    result = await services.rag.ingest(session, ingest_req)
    return result.knowledge_id, result.chunks, result.chars


@router.post("/upload", response_model=UploadStatusResponse)
async def upload_file(
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    provider: str | None = Form(default=None),
    chunk_size: int | None = Form(default=None, ge=100, le=4000),
    session: AsyncSession = Depends(get_db),
) -> UploadStatusResponse:
    """上传一个文档并直接录入知识库。

    支持：PDF、DOCX、TXT、MD、CSV、LOG（以及图片）。

    注意与其他接口不同：这里不把错抛给全局处理器，而是返 200 +
    ``status="error"``，让前端能逐条展示哪个文件为何没进去。
    """
    try:
        knowledge_id, chunks, chars = await _parse_and_ingest(
            session,
            raw=await file.read(),
            filename=file.filename or f"upload-{uuid.uuid4().hex[:8]}",
            title=title,
            provider=provider,
            chunk_size=chunk_size,
        )
    except ValueError as exc:
        return UploadStatusResponse(
            status="error", knowledge_id=None, chunks=0, error=str(exc)
        )
    except Exception as exc:  # pragma: no cover - 兼容意外错因，不让整个请求挂掉
        logger.exception("Upload failed")
        return UploadStatusResponse(
            status="error", knowledge_id=None, chunks=0, error=f"入库失败：{exc}"
        )

    return UploadStatusResponse(
        status="success",
        knowledge_id=knowledge_id,
        chunks=chunks,
        chars=chars,
        error=None,
    )


@router.post("/upload/batch", response_model=UploadBatchResponse)
async def upload_files_batch(
    files: list[UploadFile] = File(..., description="一次最多 20 个文件"),
    provider: str | None = Form(default=None),
    chunk_size: int | None = Form(default=None, ge=100, le=4000),
    session: AsyncSession = Depends(get_db),
) -> UploadBatchResponse:
    """一次请求录入多个文档。

    每个文件各自一个 savepoint，所以坏文件（格式不支持、文字太短、嵌入出错）
    只影响它自己，其余照常入库。文档标题取自文件名去后缀，所以重传一批
    会替换而不是堆出一堆重复文档。
    """
    if len(files) > 20:
        raise HTTPException(status_code=400, detail="一次最多上传 20 个文件")
    if not files:
        raise HTTPException(status_code=400, detail="请选择要上传的文件")

    results: list[UploadBatchItem] = []
    for file in files:
        filename = file.filename or f"upload-{uuid.uuid4().hex[:8]}"
        raw = await file.read()
        try:
            async with session.begin_nested():
                knowledge_id, chunks, chars = await _parse_and_ingest(
                    session,
                    raw=raw,
                    filename=filename,
                    title=None,
                    provider=provider,
                    chunk_size=chunk_size,
                )
            results.append(
                UploadBatchItem(
                    filename=filename,
                    status="success",
                    knowledge_id=knowledge_id,
                    chunks=chunks,
                    chars=chars,
                )
            )
        except ValueError as exc:
            results.append(
                UploadBatchItem(filename=filename, status="error", error=str(exc))
            )
        except Exception as exc:  # pragma: no cover - 兼容意外错因，归到单个文件
            logger.exception("Batch upload failed for %s", filename)
            results.append(
                UploadBatchItem(filename=filename, status="error", error=f"入库失败：{exc}")
            )

    succeeded = sum(1 for r in results if r.status == "success")
    return UploadBatchResponse(
        results=results,
        total=len(results),
        succeeded=succeeded,
        failed=len(results) - succeeded,
    )


@router.post("/ingest-text", response_model=KnowledgeIngestResponse)
async def ingest_text(
    title: str = Form(...),
    content: str = Form(...),
    provider: str | None = Form(default=None),
    chunk_size: int | None = Form(default=None, ge=100, le=4000),
    session: AsyncSession = Depends(get_db),
) -> KnowledgeIngestResponse:
    """直接录入一段文字（给旧版的文本输入界面用）。"""
    req = KnowledgeIngestRequest(
        title=title,
        content=content,
        provider=services.rag._resolved_provider(provider),
        chunk_size=chunk_size,
    )
    return await services.rag.ingest(session, req)
