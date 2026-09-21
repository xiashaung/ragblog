"""RAG 知识录入的文件解析：把各种格式统一成「文本 + 内嵌图片」。

支持：
- 纯文本（.txt / .md / .csv / .log）
- PDF（.pdf）：文字用 pdfminer.six，内嵌位图用 PyMuPDF
- Word（.docx）：段落 + 表格文字与行内图片都用 python-docx

两个入口的分工：``parse_document`` 保持老约定（只返文字），旧调用方与测试不受影响；
``parse_document_full`` 额外返文档**内部**的位图（PDF 里的插图、Word 里的行内图）。
上传层会把这些图交给视觉模型转成文字描述（让它们变得可检），同时留着原字节供展示。
扫描版 PDF 没有文字层，只能靠同一条路径从页面图片里把内容拉回来。
"""

from __future__ import annotations

import csv
import io
import logging
import pathlib
from dataclasses import dataclass, field
from typing import IO

logger = logging.getLogger(__name__)

# 录入流程能交给视觉模型处理的位图格式；矢量 / 冷门格式（wmf、emf、jpx）直接跳过，
# 而不是让整次解析失败
_DOC_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "bmp", "webp"}


@dataclass
class ExtractedImage:
    """文档**内部**找到的一张位图（不是单独上传的文件）。"""

    data: bytes
    ext: str = "png"
    width: int = 0
    height: int = 0

    @property
    def pixels(self) -> int:
        """像素总数，用于跳过那些小到没有内容的图标。"""
        return self.width * self.height


@dataclass
class ParsedDocument:
    """一次解析的结果：正文文字 + 同一个文件里拉出来的内嵌图片。"""

    text: str = ""
    images: list[ExtractedImage] = field(default_factory=list)


async def parse_document(file: IO[bytes], filename: str) -> str:
    """向后兼容的入口：只返提取到的文字。"""
    return (await parse_document_full(file, filename)).text


async def parse_document_full(file: IO[bytes], filename: str) -> ParsedDocument:
    """按扩展名分发，把文件解析成「文字 + 内嵌图片」。"""
    ext = pathlib.Path(filename).suffix.lstrip(".").lower()

    # --- 纯文本家族 -----------------------------------------------
    if ext in {"txt", "md", "csv", "log"}:
        text = file.read().decode("utf-8", errors="replace")
        if ext == "csv":
            text = _read_csv(io.StringIO(text))
        return ParsedDocument(text=text)

    # --- PDF -----------------------------------------------------------
    if ext == "pdf":
        return await _parse_pdf_full(file)

    # --- Word（.docx）----------------------------------------------
    if ext == "docx":
        return await _parse_docx_full(file)

    raise ValueError(
        f"不支持的文件类型 .{ext or '未知'}，支持 txt / md / csv / log / pdf / docx"
    )


# ------------------------------------------------------------------
# 纯文本辅助
# ------------------------------------------------------------------

def _read_csv(csv_file: io.StringIO) -> str:
    """读 CSV，拼成一整串文本给分块器吃。"""
    reader = csv.reader(csv_file)
    rows: list[list[str]] = []
    for row in reader:
        rows.append([cell.strip() for cell in row])
    # 朴素的 CSV 渲染：行之间空一行，列之间用 ' | '（让一行表格读起来像一句话）
    return "\n\n".join(" | ".join(row) for row in rows)


# ------------------------------------------------------------------
# PDF（文字：pdfminer.six，图片：PyMuPDF）
# ------------------------------------------------------------------

async def _parse_pdf_full(file: IO[bytes]) -> ParsedDocument:
    """PDF 走两条独立的路：文字给 pdfminer，图片给 PyMuPDF，各读一次流。"""
    text = await _extract_pdf_text(file)
    images = await _extract_pdf_images(file)
    return ParsedDocument(text=text, images=images)


async def _extract_pdf_text(file: IO[bytes]) -> str:
    """用 pdfminer.six 抽出 PDF 的文字层。"""
    from pdfminer.high_level import extract_text as _do_extract

    try:
        file.seek(0)
        # 按位置传参：pdfminer.six 的不同版本把首个参数从 ``fp`` 改名成了 ``pdf_file``，
        # 按位置传才不会绑到具体参数名上。
        text = _do_extract(file)
        return text.strip() if text else ""
    except Exception as exc:  # pragma: no cover
        logger.warning("PDF parsing failed: %s", exc)
        raise ValueError(f"PDF 解析失败：{exc}") from exc


async def _extract_pdf_images(file: IO[bytes]) -> list[ExtractedImage]:
    """用 PyMuPDF 拉出 PDF 里的内嵌位图（按 xref 去重）。

    这一步的任何失败都降级成「没图」：图片抽取绝不能打断文字录入，
    而且 PyMuPDF 本身是个可选依赖。
    """
    try:
        import pymupdf as fitz  # PyMuPDF（``fitz`` 是已废弃的旧名）
    except ImportError:  # pragma: no cover - 退回旧别名
        try:
            import fitz  # PyMuPDF
        except ImportError:
            logger.warning("PyMuPDF not installed; skipping embedded PDF images")
            return []

    try:
        file.seek(0)
        doc = fitz.open(stream=file.read(), filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - 文件损坏 / 加密都归为解析失败
        logger.warning("PDF image open failed: %s", exc)
        return []

    images: list[ExtractedImage] = []
    seen: set[int] = set()
    try:
        for page in doc:
            for info in page.get_images(full=True):
                xref = info[0]
                if xref in seen:
                    continue
                seen.add(xref)
                try:
                    pix = fitz.Pixmap(doc, xref)
                    if pix.colorspace and pix.colorspace.name not in (
                        "DeviceRGB",
                        "DeviceGray",
                    ):
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    if pix.alpha:
                        pix = fitz.Pixmap(pix, 0)  # 去掉 alpha 通道，避免 PNG 体积膨胀
                    images.append(
                        ExtractedImage(
                            data=pix.tobytes("png"),
                            ext="png",
                            width=pix.width,
                            height=pix.height,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - 单张图坏了不能拖垮整批
                    logger.debug("PDF image xref=%s failed: %s", xref, exc)
    finally:
        doc.close()
    return images


# ------------------------------------------------------------------
# Word (.docx)
# ------------------------------------------------------------------

async def _parse_docx_full(file: IO[bytes]) -> ParsedDocument:
    """从 DOCX 里抽段落 + 表格文字，再加行内图片。"""
    from docx import Document as _DocxDoc

    try:
        file.seek(0)
        doc = _DocxDoc(file)
    except Exception as exc:  # pragma: no cover
        logger.warning("DOCX parsing failed: %s", exc)
        raise ValueError(f"DOCX 解析失败：{exc}") from exc

    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    # 表格里的文字也抽取（很多文档把内容放在表格中）
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    text = "\n\n".join(parts)
    return ParsedDocument(text=text, images=_extract_docx_images(doc))


def _extract_docx_images(doc: object) -> list[ExtractedImage]:
    """从 .docx 包里收集行内图片（按字节去重，同一张图多处引用只取一次）。"""
    images: list[ExtractedImage] = []
    seen: set[bytes] = set()
    for rel in doc.part.rels.values():  # type: ignore[attr-defined]
        if "image" not in rel.reltype or rel.is_external:
            continue
        try:
            part = rel.target_part
            blob = part.blob
        except Exception:  # noqa: BLE001 - 读不出的关系条目直接跳过
            continue
        if not blob or blob in seen:
            continue
        seen.add(blob)
        ext = (getattr(part, "content_type", "") or "").split("/")[-1].lower()
        if ext == "jpeg":
            ext = "jpg"
        if ext not in _DOC_IMAGE_EXTS:
            continue  # wmf / emf / 其它矢量图无法直接喂给视觉模型
        images.append(ExtractedImage(data=blob, ext=ext))
    return images
