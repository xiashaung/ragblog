"""文件上传与知识录入路由（RAG 的写入口）。

一个接口跑完全链路：收 multi-part 文件 → 解析（PDF/DOCX/TXT/MD/CSV/LOG/HTML/图片）
→ 分块 → 向量化 → 索引入库。图片没有文字层，先让视觉模型写成中文描述再入库。

解析与向量化本身写在 :func:`services.file_ingest.parse_and_ingest`，路由只负责
「读字节 + 决定走哪条路」：

- 测试模式（``APP_TEST_MODE=true``）同步执行，响应里直接给 ``knowledge_id`` 与
  分块数，便于断言；
- 生产模式落盘原件 + 登记后台任务，立即返回 ``status="queued"`` 与 ``task_id``，
  前端轮询 ``GET /ai/knowledge/tasks/{task_id}`` 拿终态。大文档不再把 HTTP 请求
  撑到超时，也不会因为 worker 排队而重复解析。

三个入口都是「写库」（要调嵌入模型），所以统一要求 ``knowledge:manage``。
另外 ``/upload/image`` 不入库：只给 Markdown 编辑器插图用，把图落盘后返一个地址
（同样是写媒体目录，所要求的权限与录入一致）。
"""

from __future__ import annotations

import logging
import pathlib
import time
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from .. import deps, services
from ..config import get_settings
from ..db import get_db
from ..rbac import Permission
from ..schemas.ai import DocStatus
from ..schemas.upload import (
    KnowledgeIngestRequest,
    KnowledgeIngestResponse,
    UploadBatchItem,
    UploadBatchResponse,
    UploadImageResponse,
    UploadStatusResponse,
)
from ..services import file_ingest
from ..services.rbac_service import Principal

logger = logging.getLogger(__name__)
router = APIRouter()

# 常量只定义一份（在 services.file_ingest），这里转发，避免两处各改各的
IMAGE_MIME = file_ingest.IMAGE_MIME
MAX_IMAGE_BYTES = file_ingest.MAX_IMAGE_BYTES
MAX_DOCUMENT_BYTES = file_ingest.MAX_DOCUMENT_BYTES

MAX_BATCH_FILES = 20


async def _read_capped(file: UploadFile, limit: int, *, label: str = "文件") -> bytes:
    """分块读到上限为止，超限立即 abort——不是读完整包再查。

    ``await file.read()`` 会把任意大的上传体先全量搬进内存，超限检查放在读
    完之后等于没防（1 GB 的「PDF」照样打爆 worker）。这里每 1 MB 读一块，
    一过线就拒，内存占用封顶在 limit + 1MB。
    """
    buf = bytearray()
    while chunk := await file.read(1024 * 1024):
        buf += chunk
        if len(buf) > limit:
            raise ValueError(f"{label}过大，单个上限 {limit // 1024 // 1024} MB，已中止读取")
    return bytes(buf)


def _error(detail: str) -> UploadStatusResponse:
    """统一的「没入库」响应（200 + status=error，让前端逐条展示原因）。"""
    return UploadStatusResponse(
        status="error", knowledge_id=None, chunks=0, chars=0, error=detail
    )


async def _run_ingest(
    session: AsyncSession,
    *,
    raw: bytes,
    filename: str,
    title: str | None,
    provider: str | None,
    chunk_size: int | None,
) -> UploadStatusResponse:
    """同步执行解析+入库，把结果/错误翻译成响应（两条入口共用）。"""
    try:
        knowledge_id, chunks, chars = await file_ingest.parse_and_ingest(
            session,
            raw=raw,
            filename=filename,
            title=title,
            provider=provider,
            chunk_size=chunk_size,
        )
    except ValueError as exc:
        return _error(str(exc))
    except Exception as exc:  # pragma: no cover - 兼容意外错因，不让整个请求挂掉
        logger.exception("Upload failed for %s", filename)
        return _error(f"入库失败：{exc}")
    return UploadStatusResponse(
        status="success", knowledge_id=knowledge_id, chunks=chunks, chars=chars
    )


async def _enqueue_upload(
    session: AsyncSession,
    *,
    raw: bytes,
    filename: str,
    title: str | None,
    provider: str | None,
    chunk_size: int | None,
) -> UploadStatusResponse:
    """落盘原件 + 登记后台任务，立即返回 ``queued``。

    扩展名不支持时在落盘前就拒（否则白写一个文件）；登记失败时删掉刚落盘原件，
    否则 ``uploads/`` 里会堆出一堆没有任何任务指向的孤儿文件（媒体回收器查不到
    它们，只能靠这里清理）。
    """
    ext = file_ingest.ext_of(filename)
    if not services.document_parser.is_supported(filename):
        return _error(services.document_parser.unsupported_message(filename))

    media_path = file_ingest.save_upload_bytes(raw, ext)
    try:
        task_id = await services.rag_queue.get_queue().enqueue(
            session,
            services.rag_queue.RAGTask(
                task_type=services.rag_queue.TaskType.UPLOAD,
                payload={
                    "media_path": media_path,
                    "filename": filename,
                    "title": title,
                    "provider": provider,
                    "chunk_size": chunk_size,
                },
                created_at=time.time(),
            ),
        )
    except Exception as exc:  # noqa: BLE001 - 登记失败要清原件，不能留垃圾
        path = pathlib.Path(get_settings().media_root) / media_path
        try:
            path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - 清理失败不影响报错本身
            logger.warning("清理未登记的原上传件失败 %s", path)
        logger.exception("上传任务登记失败：%s", filename)
        return _error(f"后台任务登记失败：{exc}")

    return UploadStatusResponse(status="queued", task_id=task_id)


@router.post("/upload/image", response_model=UploadImageResponse)
async def upload_image(
    file: UploadFile = File(...),
    _principal: Principal = Depends(deps.require_permission(Permission.KNOWLEDGE_MANAGE)),
) -> UploadImageResponse:
    """存一张图片到媒体目录，只返地址（不进知识库）。

    给 Markdown 编辑器的插图与粘贴截图用：正文里只写 ``![](/media/uploads/x.png)``，
    base64 大 blob 不会混进分块与向量。不要求对话模型支持视觉，
    也不会新建文档 —— 图只是正文的一部分。
    """
    filename = file.filename or "image"
    ext = file_ingest.ext_of(filename) or "png"
    if ext not in IMAGE_MIME:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的图片格式（.{ext}），仅支持 {' / '.join(IMAGE_MIME)}",
        )
    raw = await _read_capped(file, MAX_IMAGE_BYTES, label="图片")
    if not raw:
        raise HTTPException(status_code=400, detail="图片内容为空")
    url = file_ingest.save_image_file(raw, ext)
    return UploadImageResponse(url=url, name=filename, bytes=len(raw))


@router.post("/upload", response_model=UploadStatusResponse)
async def upload_file(
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    provider: str | None = Form(default=None),
    chunk_size: int | None = Form(default=None, ge=100, le=4000),
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.KNOWLEDGE_MANAGE)),
) -> UploadStatusResponse:
    """上传一个文档并录入知识库。

    支持：PDF、DOCX、TXT、MD、CSV、LOG、HTML（以及图片）。

    注意与其他接口不同：这里不把错抛给全局处理器，而是返 200 +
    ``status="error"``，让前端能逐条展示哪个文件为何没进去。生产模式返
    ``status="queued"`` + ``task_id``，真正的结果在后台任务里。
    """
    filename = file.filename or f"upload-{uuid.uuid4().hex[:8]}"
    try:
        raw = await _read_capped(file, MAX_DOCUMENT_BYTES)
    except ValueError as exc:
        return _error(str(exc))

    if services.rag_queue.sync_mode():
        return await _run_ingest(
            session,
            raw=raw,
            filename=filename,
            title=title,
            provider=provider,
            chunk_size=chunk_size,
        )
    return await _enqueue_upload(
        session,
        raw=raw,
        filename=filename,
        title=title,
        provider=provider,
        chunk_size=chunk_size,
    )


@router.post("/upload/batch", response_model=UploadBatchResponse)
async def upload_files_batch(
    files: list[UploadFile] = File(..., description="一次最多 20 个文件"),
    provider: str | None = Form(default=None),
    chunk_size: int | None = Form(default=None, ge=100, le=4000),
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.KNOWLEDGE_MANAGE)),
) -> UploadBatchResponse:
    """一次请求录入多个文档。

    每个文件各自一个 savepoint，所以坏文件（格式不支持、文字太短、嵌入出错）
    只影响它自己，其余照常入库。文档标题取自文件名去后缀，所以重传一批
    会替换而不是堆出一堆重复文档。
    """
    if len(files) > MAX_BATCH_FILES:
        raise HTTPException(status_code=400, detail=f"一次最多上传 {MAX_BATCH_FILES} 个文件")
    if not files:
        raise HTTPException(status_code=400, detail="请选择要上传的文件")

    batch_limit = get_settings().upload_batch_max_bytes
    results: list[UploadBatchItem] = []
    queued = 0
    total_bytes = 0

    for file in files:
        filename = file.filename or f"upload-{uuid.uuid4().hex[:8]}"
        try:
            raw = await _read_capped(file, MAX_DOCUMENT_BYTES)
            if total_bytes + len(raw) > batch_limit:
                raise ValueError(
                    f"批量上传总量过大（一次上限 {batch_limit // 1024 // 1024} MB）"
                )
            total_bytes += len(raw)
        except ValueError as exc:
            results.append(UploadBatchItem(filename=filename, status="error", error=str(exc)))
            continue

        if services.rag_queue.sync_mode():
            try:
                async with session.begin_nested():
                    knowledge_id, chunks, chars = await file_ingest.parse_and_ingest(
                        session,
                        raw=raw,
                        filename=filename,
                        title=None,
                        provider=provider,
                        chunk_size=chunk_size,
                    )
            except ValueError as exc:
                results.append(
                    UploadBatchItem(filename=filename, status="error", error=str(exc))
                )
                continue
            except Exception as exc:  # pragma: no cover - 兼容意外错因，归到单个文件
                logger.exception("Batch upload failed for %s", filename)
                results.append(
                    UploadBatchItem(filename=filename, status="error", error=f"入库失败：{exc}")
                )
                continue
            results.append(
                UploadBatchItem(
                    filename=filename,
                    status="success",
                    knowledge_id=knowledge_id,
                    chunks=chunks,
                    chars=chars,
                )
            )
        else:
            item = await _enqueue_upload(
                session,
                raw=raw,
                filename=filename,
                title=None,
                provider=provider,
                chunk_size=chunk_size,
            )
            if item.status == "queued":
                queued += 1
            results.append(UploadBatchItem(filename=filename, **item.model_dump()))

    succeeded = sum(1 for r in results if r.status == "success")
    return UploadBatchResponse(
        results=results,
        total=len(results),
        succeeded=succeeded,
        failed=len(results) - succeeded - queued,
        queued=queued,
    )


@router.post("/ingest-text", response_model=KnowledgeIngestResponse)
async def ingest_text(
    title: str = Form(...),
    content: str = Form(...),
    provider: str | None = Form(default=None),
    chunk_size: int | None = Form(default=None, ge=100, le=4000),
    session: AsyncSession = Depends(get_db),
    _principal: Principal = Depends(deps.require_permission(Permission.KNOWLEDGE_MANAGE)),
) -> KnowledgeIngestResponse:
    """直接录入一段文字（给旧版的文本输入界面用）。

    与 ``POST /ai/knowledge/ingest`` 走同一条路：测试模式同步执行，生产模式登记
    后台任务并立即返回 ``pending`` + ``task_id``。这个老入口不能绕过队列，否则
    长正文照样把 HTTP 请求撑到超时。
    """
    req = KnowledgeIngestRequest(
        title=title,
        content=content,
        provider=services.rag._resolved_provider(provider),
        chunk_size=chunk_size,
    )
    if services.rag_queue.sync_mode():
        return await services.rag.ingest(session, req)

    task_id = await services.rag_queue.get_queue().enqueue(
        session,
        services.rag_queue.RAGTask(
            task_type=services.rag_queue.TaskType.INGEST,
            payload=req.model_dump(),
            created_at=time.time(),
        ),
    )
    return KnowledgeIngestResponse(
        knowledge_id=f"pending-{task_id}",
        status=DocStatus.PENDING,
        chunks=0,
        chars=0,
        task_id=task_id,
    )
