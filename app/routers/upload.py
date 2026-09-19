"""File upload + knowledge ingestion for RAG.

Supports multi-part form upload of documents (PDF, DOCX, TXT, MD, CSV, LOG).
The uploaded file is parsed, chunked, embedded, and indexed in the knowledge
base in a single API call.
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

# Image formats we accept for knowledge ingestion. Images cannot be embedded by
# the text-only embedding model, so at ingest time a vision-capable chat model
# writes a textual description that becomes the searchable document body; the
# original image is saved to the media directory and referenced (by URL) in
# ``metadata["images"]`` so the browser can show it without a DB blob.
IMAGE_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
}
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB per image, keeps the JSON meta bounded
# Document-embedded images (figures inside a PDF/DOCX) are described one by one,
# which costs a vision-model call each; cap the count and skip decoration.
MAX_EMBEDDED_IMAGES = 6  # describe at most N embedded images per document
MIN_EMBEDDED_PIXELS = 6_400  # skip tiny icons/logos (e.g. 80x80)

_IMAGE_PROMPT = (
    "请用中文详细描述这张图片的内容，包括主体、文字、图表数据与场景，"
    "以便后续据此检索与问答。只输出描述文本，不要额外解释。"
)


def _image_data_url(raw: bytes, filename: str) -> str:
    ext = pathlib.Path(filename).suffix.lstrip(".").lower()
    mime = IMAGE_MIME.get(ext, "image/png")
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _bytes_data_url(data: bytes, ext: str) -> str:
    """Data URL for an embedded image already decoded to bytes (ext like 'png')."""
    mime = IMAGE_MIME.get(ext, "image/png")
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _save_image_file(data: bytes, ext: str) -> str:
    """Persist image bytes to the media dir and return a browser-resolvable URL.

    The blob goes to disk (not the database); ``metadata["images"]`` stores the
    returned ``/media/uploads/<uuid>.<ext>`` path so the SPA can display it
    directly. Vision calls still use an inline data URL built from the bytes in
    hand, so a local model that cannot fetch our URL keeps working.
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
    """Describe document-embedded images with the vision model.

    Returns ``(descriptions, image_urls)`` — ``image_urls`` are on-disk media
    URLs (not blobs).  Tiny/decorative images are skipped, each image's failure
    is tolerated (one bad picture must not abort the whole upload), and both the
    number described and the number persisted are capped so a picture-heavy
    document stays bounded.
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
        except Exception as exc:  # noqa: BLE001 - skip this image, keep going
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
    """Ask the configured (vision) chat model to describe an image, return text."""
    backend = get_llm(services.rag._resolved_provider(provider))
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
    """Parse one upload and ingest it; raises ``ValueError`` with a Chinese message.

    Returns ``(knowledge_id, chunks, chars)``. Kept free of any HTTP concern so
    the single-file and batch endpoints share one code path.
    """
    if not raw:
        raise ValueError("文件内容为空")

    ext = pathlib.Path(filename).suffix.lstrip(".").lower()
    doc_title = (title or pathlib.Path(filename).stem or "Unknown Document").strip()[:200]

    # --- Image: describe it with a vision model, keep the bytes for display ---
    if ext in IMAGE_MIME:
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError(
                f"图片过大（{len(raw) // 1024 // 1024} MB），"
                f"单张上限 {MAX_IMAGE_BYTES // 1024 // 1024} MB"
            )
        data_url = _image_data_url(raw, filename)  # 仅用于喂视觉模型
        try:
            description = await _describe_image(data_url, provider)
        except Exception as exc:  # noqa: BLE001 - surface model/format errors per file
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

    # --- Text-bearing documents (incl. images embedded inside PDF / DOCX) ---
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
    """Upload a document and ingest it into the knowledge base.

    Supports: PDF, DOCX, TXT, MD, CSV, LOG.
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
    except Exception as exc:  # pragma: no cover - defensive
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
    """Ingest several documents in one request.

    Each file gets its own savepoint, so a broken file (unsupported type, too
    short, embedding error) is reported while the good ones stay ingested. A
    document is titled after its filename stem, so re-uploading a batch
    replaces the same documents rather than duplicating them.
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
        except Exception as exc:  # pragma: no cover - defensive
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
    """Direct text ingestion (for the legacy text-input UI)."""
    req = KnowledgeIngestRequest(
        title=title,
        content=content,
        provider=services.rag._resolved_provider(provider),
        chunk_size=chunk_size,
    )
    return await services.rag.ingest(session, req)
