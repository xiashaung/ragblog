"""文件上传相关模型（RAG 录入）。

注意：这里只放上传结果类型；录入请求/响应本体在 ``schemas.ai``，
仍从这里 re-export 一份，避免「文件上传」与「文本录入」两个入口的形状飘开。
"""

from pydantic import BaseModel, Field

# 录入请求 / 响应只在 ``schemas.ai`` 里定义一份，这里转发出去，
# 两个入口不可能各自改坏
from .ai import KnowledgeIngestRequest, KnowledgeIngestResponse

__all__ = [
    "KnowledgeIngestRequest",
    "KnowledgeIngestResponse",
    "UploadBatchItem",
    "UploadBatchResponse",
    "UploadImageResponse",
    "UploadStatusResponse",
]


class UploadImageResponse(BaseModel):
    """只落盘一张图、不入库：编辑器插图与粘贴截图拿的是一个可嵌入 Markdown 的地址。"""

    url: str = Field(..., description="浏览器可直接访问的媒体地址，形如 /media/uploads/x.png")
    name: str = Field(default="", description="文件名，给编辑器当 alt 文本用")
    bytes: int = 0


class UploadStatusResponse(BaseModel):
    """单文件上传接口的响应（成功与失败都是 200，看 ``status``）。"""

    status: str = Field(
        ...,
        description="success 或 error",
        pattern="^(success|error)$",
    )
    knowledge_id: str | None = None
    chunks: int = 0
    chars: int = 0
    error: str | None = None


class UploadBatchItem(UploadStatusResponse):
    """批量上传里单个文件的结果（标题取自文件名）。"""

    filename: str = ""


class UploadBatchResponse(BaseModel):
    """逐文件结果 + 计数；部分成功是预期行为，不算错。"""

    results: list[UploadBatchItem] = []
    total: int = 0
    succeeded: int = 0
    failed: int = 0
