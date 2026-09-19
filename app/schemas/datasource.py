"""外部数据源（智能问数的可选目标库）的增删改查契约。

安全约定：连接串 ``url`` 含口令，**任何响应都不回原文**，只回掩码串
``url_masked``。更新时 ``url`` 留空表示沿用旧值。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

__all__ = [
    "DataSourceCreate",
    "DataSourceOut",
    "DataSourceTestRequest",
    "DataSourceTestResult",
    "DataSourceUpdate",
]

#: 目前支持的方言（决定用哪个异步驱动、连接串怎么归一）。
DATA_SOURCE_DIALECTS = ("sqlite", "mysql", "postgresql")


class DataSourceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    dialect: str = Field(..., description="sqlite | mysql | postgresql")
    url: str = Field(
        ...,
        min_length=1,
        description=(
            "连接串，如 mysql://用户:口令@主机:3306/库 或 sqlite:////绝对/路径.db；"
            "同步驱动会自动补成异步驱动"
        ),
    )
    readonly: bool = Field(default=True, description="是否强制只读连接")
    enabled: bool = True
    description: str = Field(default="", max_length=200)


class DataSourceUpdate(BaseModel):
    """所有字段可选；``url`` 留空表示不修改连接串。"""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    dialect: str | None = None
    url: str | None = None
    readonly: bool | None = None
    enabled: bool | None = None
    description: str | None = Field(default=None, max_length=200)


class DataSourceOut(BaseModel):
    id: str
    name: str
    dialect: str
    url_masked: str = Field(default="", description="掩码后的连接串，永不含明文口令")
    readonly: bool = True
    enabled: bool = True
    description: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0


class DataSourceTestRequest(BaseModel):
    """测试连通性：可传新建前的草稿（带 id 则允许 url 留空沿用已存的）。"""

    dialect: str = Field(..., description="sqlite | mysql | postgresql")
    url: str = Field(default="", description="留空且带 existing_id 时沿用库中已存连接串")
    existing_id: str | None = None


class DataSourceTestResult(BaseModel):
    ok: bool
    detail: str = ""
    table_count: int = -1
