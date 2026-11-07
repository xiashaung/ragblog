"""文件录入的共用实现：解析 → 图片描述 → 分块向量化 → 入库。

以前这条链路写在路由里（``app/routers/upload.py``），于是「上传」只能同步跑：
一次请求里解析 PDF、调视觉模型描述内嵌图、再逐片嵌入，大文档把 HTTP 请求
撑到前端超时（见 ``rag_queue`` 的后台队列）。搬到服务层后，同步入口与 Worker
用的是同一份代码，两侧的结果与报错文案不可能各自漂移。

图片没有文字层，所以录入时先让**对话模型**（不是请求里那个 embedding provider）
写一段中文描述当可检索正文；原图落盘到媒体目录，库里只存 URL，不埋 base64。

三个闸门（都可通过配置调）：
- 单文档字节上限 ``MAX_DOCUMENT_BYTES``、单图 ``MAX_IMAGE_BYTES``；
- 每篇文档最多描述 ``max_embedded_images`` 张内嵌图，且总共最多尝试
  ``max_image_description_attempts`` 次视觉调用 —— 失败的图也算尝试，否则
  一百张坏图 = 一百次白付的模型调用；
- 归一化后的正文字符上限 ``max_ingest_chars``（在 :func:`rag.upsert_document` 里拦）。
"""

from __future__ import annotations

import base64
import io
import logging
import pathlib
import time
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..schemas.ai import ChatMessage, KnowledgeIngestRequest
from . import document_parser, rag
from .llm import get_llm

logger = logging.getLogger(__name__)

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
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024  # 单个文档 20 MB：解析器全量吃进内存，必须封顶

_UPLOAD_SUFFIX = "uploads"

_IMAGE_PROMPT = (
    "请用中文详细描述这张图片的内容，包括主体、文字、图表数据与场景，"
    "以便后续据此检索与问答。只输出描述文本，不要额外解释。"
)


def ext_of(filename: str) -> str:
    """文件扩展名（小写、不带点）；没有扩展名时返空串。"""
    return pathlib.Path(filename).suffix.lstrip(".").lower()


def save_image_file(data: bytes, ext: str) -> str:
    """图片字节落盘到媒体目录，返一个浏览器能直接访问的 URL。

    字节只进磁盘不进库：``metadata["images"]`` 存的是返回的
    ``/media/uploads/<uuid>.<ext>`` 路径，SPA 拿去直接展示。喂视觉模型时仍然用
    手上字节现拼的内联 data URL，所以访问不到本服务 URL 的本地模型也照样能用。
    """
    settings = get_settings()
    safe_ext = ext if ext in IMAGE_MIME else "png"
    uploads = pathlib.Path(settings.media_root) / _UPLOAD_SUFFIX
    uploads.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}.{safe_ext}"
    (uploads / name).write_bytes(data)
    return f"{settings.media_url.rstrip('/')}/{_UPLOAD_SUFFIX}/{name}"


def save_upload_bytes(raw: bytes, ext: str) -> str:
    """把上传原件落盘，返相对于媒体目录的路径（``uploads/<uuid>.<ext>``）。

    异步录入走后台队列：HTTP 请求只落文件 + 登记任务，解析与向量化在 Worker 里
    做，Worker 读完即删 —— 所以这里落的是**原件**，不是最终展示用的图片。
    """
    settings = get_settings()
    uploads = pathlib.Path(settings.media_root) / _UPLOAD_SUFFIX
    uploads.mkdir(parents=True, exist_ok=True)
    safe_ext = ext or "bin"
    name = f"{uuid.uuid4().hex}.{safe_ext}"
    (uploads / name).write_bytes(raw)
    return f"{_UPLOAD_SUFFIX}/{name}"


def image_data_url(raw: bytes, filename: str) -> str:
    """按文件扩展名猜 MIME，把字节包成内联 data URL（只用来喂模型）。"""
    mime = IMAGE_MIME.get(ext_of(filename), "image/png")
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def bytes_data_url(data: bytes, ext: str) -> str:
    """给已经解成字节的内嵌图拼 data URL（``ext`` 形如 ``'png'``）。"""
    mime = IMAGE_MIME.get(ext, "image/png")
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


async def describe_image(data_url: str, provider: str | None = None) -> str:
    """让配置里的（视觉）对话模型描述一张图，返文字。

    图片描述是「对话」能力，所以用系统配置里的 chat_provider；请求里的
    ``provider`` 是 embedding provider，拿它当视觉模型会选错后端（很多向量模型
    不支持图片输入），因此这里一律不传 —— 参数保留只是为了调用形状不变。
    """
    backend = get_llm(None, feature="chat")
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


def _drop_saved(urls: list[str]) -> None:
    """录入失败时删掉已落盘的图片，别让孤儿文件留在媒体目录。"""
    from .. import media_gc

    for url in urls:
        path = media_gc.url_to_path(url)
        if path is None:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("清理未入库的图片失败 %s：%s", path, exc)


async def describe_embedded_images(
    images: list[document_parser.ExtractedImage], provider: str | None = None
) -> tuple[list[str], list[str], dict[str, int]]:
    """用视觉模型描述文档内嵌图，返 ``(描述列表, 图片URL列表, 统计)``。

    ``图片URL`` 都是磁盘上的媒体地址（不是 blob）。过小 / 装饰图直接跳过，单张失败
    也只跳不抛（一张坏图不能拖垮整个上传）；描述次数与落盘次数都封顶，
    图多的文档不至于把入库时间拉成不可控。
    """
    settings = get_settings()
    max_images = max(0, settings.max_embedded_images)
    max_attempts = max(0, settings.max_image_description_attempts)
    min_pixels = settings.min_embedded_pixels

    descriptions: list[str] = []
    urls: list[str] = []
    stats = {
        "described": 0,
        "skipped_small": 0,
        "skipped_oversized": 0,
        "failed": 0,
        "attempts": 0,
    }
    for idx, img in enumerate(images, 1):
        if stats["described"] >= max_images:
            break
        if stats["attempts"] >= max_attempts:
            logger.warning(
                "内嵌图片描述达到上限（%d 次尝试），剩余 %d 张跳过",
                max_attempts,
                len(images) - idx + 1,
            )
            break
        # ``pixels`` 为 0 时（DOCX 行内图不带尺寸）不能当成「小图」跳过
        if img.pixels and img.pixels < min_pixels:
            stats["skipped_small"] += 1
            continue
        if not img.data or len(img.data) > MAX_IMAGE_BYTES:
            stats["skipped_oversized"] += 1
            continue
        data_url = bytes_data_url(img.data, img.ext)  # 仅用于喂视觉模型
        stats["attempts"] += 1
        try:
            desc = await describe_image(data_url, provider)
        except Exception as exc:  # noqa: BLE001 - 这张跳过，继续后面的
            logger.warning("描述内嵌图片失败（已跳过第 %s 张）：%s", idx, exc)
            stats["failed"] += 1
            continue
        stats["described"] += 1
        if desc and len(desc.strip()) >= 5:
            descriptions.append(f"[图片{idx}] {desc.strip()}")
            if len(urls) < max_images:
                try:
                    urls.append(save_image_file(img.data, img.ext))
                except OSError as exc:  # 落盘失败不影响文字入库
                    logger.warning("保存内嵌图片失败（第 %s 张）：%s", idx, exc)
    return descriptions, urls, stats


async def parse_and_ingest(
    session: AsyncSession,
    *,
    raw: bytes,
    filename: str,
    title: str | None,
    provider: str | None,
    chunk_size: int | None,
) -> tuple[str, int, int]:
    """解析一个上传件并入库；失败抛 ``ValueError``（中文描述）。

    返 ``(knowledge_id, 分块数, 字数)``。里层不碰任何 HTTP 概念，单文件、批量
    两个入口和后台队列 Worker 才能共用这一条路径。
    """
    if not raw:
        raise ValueError("文件内容为空")

    ext = ext_of(filename)
    if not document_parser.is_supported(filename):
        raise ValueError(document_parser.unsupported_message(filename))

    doc_title = (title or pathlib.Path(filename).stem or "Unknown Document").strip()[:200]
    saved: list[str] = []

    try:
        # --- 图片：用视觉模型转描述，字节留下来供展示 ---
        if ext in IMAGE_MIME:
            if len(raw) > MAX_IMAGE_BYTES:
                raise ValueError(
                    f"图片过大（{len(raw) // 1024 // 1024} MB），"
                    f"单张上限 {MAX_IMAGE_BYTES // 1024 // 1024} MB"
                )
            data_url = image_data_url(raw, filename)  # 仅用于喂视觉模型
            try:
                description = await describe_image(data_url, None)
            except Exception as exc:  # noqa: BLE001 - 模型/格式错误按单文件上报
                raise ValueError(
                    f"图片识别失败（需配置支持视觉的对话模型）：{exc}"
                ) from exc
            if not description or len(description.strip()) < 10:
                raise ValueError(
                    "视觉模型未生成有效图片描述，请确认后端配置的是支持视觉的对话模型"
                )
            image_url = save_image_file(raw, ext)
            saved.append(image_url)
            ingest_req = KnowledgeIngestRequest(
                title=doc_title,
                content=description,
                provider=rag._resolved_provider(provider),
                chunk_size=chunk_size,
                metadata={
                    "kind": "image",
                    "source_file": filename,
                    "images": [image_url],
                    "image_storage": "file",
                    "uploaded_at": time.time(),
                },
            )
            result = await rag.ingest(session, ingest_req)
            return result.knowledge_id, result.chunks, result.chars

        # --- 含文本的文档（包括 PDF / DOCX 内嵌的图）---
        parsed = await document_parser.parse_document_full(io.BytesIO(raw), filename)
        text = (parsed.text or "").strip()

        image_urls: list[str] = []
        descriptions: list[str] = []
        stats: dict[str, int] = {}
        if parsed.images:
            descriptions, image_urls, stats = await describe_embedded_images(
                parsed.images, None
            )
            saved.extend(image_urls)
        if descriptions:
            text = f"{text}\n\n" + "\n".join(descriptions) if text else "\n".join(descriptions)
            text = text.strip()

        if not text or len(text) < 10:
            raise ValueError(
                f"提取文本过短（{len(text)} 字），可能是扫描版 PDF 或格式不支持"
            )

        metadata: dict[str, Any] = {
            "kind": "file",
            "source_file": filename,
            "uploaded_at": time.time(),
        }
        if parsed.images:
            metadata["embedded_images"] = len(parsed.images)
            for key, value in (
                ("skipped_small_images", stats.get("skipped_small", 0)),
                ("oversized_images", stats.get("skipped_oversized", 0)),
                ("failed_images", stats.get("failed", 0)),
                ("image_description_attempts", stats.get("attempts", 0)),
            ):
                if value:
                    metadata[key] = value
        if descriptions:
            metadata["described_images"] = len(descriptions)
        if image_urls:
            metadata["images"] = image_urls

        ingest_req = KnowledgeIngestRequest(
            title=doc_title,
            content=text,
            # .md 存的本就是 Markdown 源码，标出来详情页才值得按语法渲染；
            # .html 的富文本由归一化器压成 Markdown；PDF / DOCX 抽出的是线性纯文本
            # （排版信息已经丢了），按纯文本算
            format=(
                "markdown"
                if ext == "md"
                else "html"
                if ext in {"html", "htm"}
                else "text"
            ),
            provider=rag._resolved_provider(provider),
            chunk_size=chunk_size,
            metadata=metadata,
        )
        result = await rag.ingest(session, ingest_req)
        return result.knowledge_id, result.chunks, result.chars
    except Exception:
        _drop_saved(saved)
        raise
