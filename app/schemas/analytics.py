"""智能问数（Text-to-SQL）的数据契约。

与 RAG 完全不同：这里分析的是**数据库表**本身，输入是一个问题 + 目标数据源，
输出是「生成的 SQL + 查询结果 + AI 结论」。请求带 ``source`` 选择分析哪个库
（``main`` 或某个已登记的外部数据源 id）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .ai import Provider, Usage

__all__ = [
    "AnalyticsColumn",
    "AnalyticsHistoryItem",
    "AnalyticsHistoryResponse",
    "AnalyticsQueryRequest",
    "AnalyticsQueryResponse",
    "AnalyticsSchemaResponse",
    "AnalyticsSource",
    "AnalyticsTable",
]


class AnalyticsColumn(BaseModel):
    """一列的元信息（自省所得，不是真实数据）。"""

    name: str
    type: str
    nullable: bool = True
    comment: str = ""  # 列注释（业务含义），帮模型理解该用什么表/字段


class AnalyticsTable(BaseModel):
    """一张表：名字 + 注释 + 列清单，也是喂进 prompt 的最小单位。"""

    name: str
    comment: str = ""  # 表注释（如「项目任务表」），选表的关键语义依据
    columns: list[AnalyticsColumn] = Field(default_factory=list)


class AnalyticsSchemaResponse(BaseModel):
    """可问数的表结构（凭证列已剔除）。"""

    dialect: str
    database: str = ""
    source: str = "main"
    tables: list[AnalyticsTable] = Field(default_factory=list)


class AnalyticsSource(BaseModel):
    """问数页下拉可选的数据源（只暴露选择用的最小信息，不含连接串）。"""

    key: str = Field(..., description='"main"（本应用主库）或数据源 id')
    name: str
    dialect: str = ""
    builtin: bool = False


class AnalyticsQueryRequest(BaseModel):
    """一次问数的入参：问题必填，数据源与后端可选（默认主库）。"""

    question: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="用自然语言描述你想分析什么",
    )
    provider: Provider | None = Field(
        default=None, description="留空则用服务端默认大模型后端"
    )
    source: str = Field(
        default="main",
        description='要分析的数据源："main"（本应用主库）或数据源 id',
    )
    dry_run: bool = Field(
        default=False,
        description="只生成并校验 SQL，不执行、不出结论（安全预览模式）",
    )


class AnalyticsQueryResponse(BaseModel):
    """问数结果：SQL、数据、结论；任一环节失败时 error 给出友好中文，仍返回 200。"""

    question: str
    provider: Provider
    source: str = "main"
    dialect: str = ""
    sql: str = ""
    brief: str = ""
    columns: list[str] = Field(default_factory=list)
    rows: list[list[object]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    conclusion: str = ""
    error: str | None = None
    usage: Usage | None = None
    chat_id: str = ""  # 落库后的历史消息 id（前端本地追加用）
    created_at: float = 0.0  # 落库时间戳（秒）


class AnalyticsHistoryItem(BaseModel):
    """一条可回放的问数历史（存了完整的问数响应，重建卡片即用）。"""

    id: str
    question: str
    source: str
    created_at: float
    result: AnalyticsQueryResponse


class AnalyticsHistoryResponse(BaseModel):
    """GET /analytics/history 返回（包成对象才走统一信封，数组不包）。"""

    messages: list[AnalyticsHistoryItem] = Field(default_factory=list)
