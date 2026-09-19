"""Upload-related schemas for RAG file ingestion."""

from pydantic import BaseModel, Field

# The ingest request/response shapes live in ``schemas.ai`` and are re-exported
# here so the file-upload and text-ingest entry points cannot drift apart.
from .ai import KnowledgeIngestRequest, KnowledgeIngestResponse

__all__ = [
    "KnowledgeIngestRequest",
    "KnowledgeIngestResponse",
    "UploadBatchItem",
    "UploadBatchResponse",
    "UploadStatusResponse",
]


class UploadStatusResponse(BaseModel):
    """Response from file upload endpoint."""

    status: str = Field(
        ...,
        description="'success' or 'error'",
        pattern="^(success|error)$",
    )
    knowledge_id: str | None = None
    chunks: int = 0
    chars: int = 0
    error: str | None = None


class UploadBatchItem(UploadStatusResponse):
    """Outcome of one file in a batch upload (title comes from the filename)."""

    filename: str = ""


class UploadBatchResponse(BaseModel):
    """Per-file results; partial success is expected, not an error."""

    results: list[UploadBatchItem] = []
    total: int = 0
    succeeded: int = 0
    failed: int = 0
