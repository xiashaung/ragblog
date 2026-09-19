"""Document parser for RAG knowledge ingestion.

Supports:
- Plain text (.txt, .md, .csv, .log)
- Markdown (.md)
- PDF (.pdf)    text via pdfminer.six, embedded images via PyMuPDF
- Word (.docx)  text (paragraphs + tables) and inline images via python-docx

``parse_document`` keeps the old contract (return the extracted text only) so
existing callers and tests are unaffected.  ``parse_document_full`` additionally
returns the raster images embedded *inside* the document (figures in a PDF,
inline pictures in a Word file); the upload layer feeds those to a vision model
to obtain a textual description — making them searchable — and keeps the bytes
for display.  A scanned PDF has no text layer, so it is recovered entirely from
its page images via that same path.
"""

from __future__ import annotations

import csv
import io
import logging
import pathlib
from dataclasses import dataclass, field
from typing import IO

logger = logging.getLogger(__name__)

# Raster formats the ingest pipeline can hand to a vision model. Vector /
# exotic formats (wmf, emf, jpx) are skipped rather than aborting the parse.
_DOC_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "bmp", "webp"}


@dataclass
class ExtractedImage:
    """One raster image found *inside* a document (not a standalone upload)."""

    data: bytes
    ext: str = "png"
    width: int = 0
    height: int = 0

    @property
    def pixels(self) -> int:
        return self.width * self.height


@dataclass
class ParsedDocument:
    """Text body plus any embedded images pulled out of the same file."""

    text: str = ""
    images: list[ExtractedImage] = field(default_factory=list)


async def parse_document(file: IO[bytes], filename: str) -> str:
    """Back-compatible helper: return only the extracted text."""
    return (await parse_document_full(file, filename)).text


async def parse_document_full(file: IO[bytes], filename: str) -> ParsedDocument:
    """Parse *file* into text + embedded images, dispatching on the extension."""
    ext = pathlib.Path(filename).suffix.lstrip(".").lower()

    # --- Plain-text families -------------------------------------------
    if ext in {"txt", "md", "csv", "log"}:
        text = file.read().decode("utf-8", errors="replace")
        if ext == "csv":
            text = _read_csv(io.StringIO(text))
        return ParsedDocument(text=text)

    # --- PDF -----------------------------------------------------------
    if ext == "pdf":
        return await _parse_pdf_full(file)

    # --- Word (.docx) --------------------------------------------------
    if ext == "docx":
        return await _parse_docx_full(file)

    raise ValueError(
        f"不支持的文件类型 .{ext or '未知'}，支持 txt / md / csv / log / pdf / docx"
    )


# ------------------------------------------------------------------
# Plain-text helpers
# ------------------------------------------------------------------

def _read_csv(csv_file: io.StringIO) -> str:
    """Read a CSV and return the content as a single text string."""
    reader = csv.reader(csv_file)
    rows: list[list[str]] = []
    for row in reader:
        rows.append([cell.strip() for cell in row])
    # simple CSV rendering: rows separated by blank line, columns by ' | '
    return "\n\n".join(" | ".join(row) for row in rows)


# ------------------------------------------------------------------
# PDF (text: pdfminer.six, images: PyMuPDF)
# ------------------------------------------------------------------

async def _parse_pdf_full(file: IO[bytes]) -> ParsedDocument:
    text = await _extract_pdf_text(file)
    images = await _extract_pdf_images(file)
    return ParsedDocument(text=text, images=images)


async def _extract_pdf_text(file: IO[bytes]) -> str:
    """Extract the text layer from a PDF using pdfminer.six."""
    from pdfminer.high_level import extract_text as _do_extract

    try:
        file.seek(0)
        # positional: the first param was renamed ``fp`` -> ``pdf_file`` across
        # pdfminer.six versions, so passing it by position keeps this portable.
        text = _do_extract(file)
        return text.strip() if text else ""
    except Exception as exc:  # pragma: no cover
        logger.warning("PDF parsing failed: %s", exc)
        raise ValueError(f"PDF 解析失败：{exc}") from exc


async def _extract_pdf_images(file: IO[bytes]) -> list[ExtractedImage]:
    """Pull embedded raster images out of a PDF with PyMuPDF (dedup by xref).

    Every failure here degrades to "no images" — image extraction must never
    break text ingestion, and PyMuPDF is an optional dependency.
    """
    try:
        import pymupdf as fitz  # PyMuPDF (``fitz`` is the deprecated old name)
    except ImportError:  # pragma: no cover - fall back to the legacy alias
        try:
            import fitz  # PyMuPDF
        except ImportError:
            logger.warning("PyMuPDF not installed; skipping embedded PDF images")
            return []

    try:
        file.seek(0)
        doc = fitz.open(stream=file.read(), filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - corrupt/encrypted pdf
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
                        pix = fitz.Pixmap(pix, 0)  # drop alpha to keep PNG small
                    images.append(
                        ExtractedImage(
                            data=pix.tobytes("png"),
                            ext="png",
                            width=pix.width,
                            height=pix.height,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - one bad image must not abort
                    logger.debug("PDF image xref=%s failed: %s", xref, exc)
    finally:
        doc.close()
    return images


# ------------------------------------------------------------------
# Word (.docx)
# ------------------------------------------------------------------

async def _parse_docx_full(file: IO[bytes]) -> ParsedDocument:
    """Extract paragraphs + table text, plus inline images, from a DOCX."""
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
    """Collect inline images from the .docx package (deduped by bytes)."""
    images: list[ExtractedImage] = []
    seen: set[bytes] = set()
    for rel in doc.part.rels.values():  # type: ignore[attr-defined]
        if "image" not in rel.reltype or rel.is_external:
            continue
        try:
            part = rel.target_part
            blob = part.blob
        except Exception:  # noqa: BLE001 - unreadable relationship part
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
