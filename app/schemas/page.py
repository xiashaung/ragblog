"""列表接口统一分页：一个查询参数依赖 + 一个泛型分页响应。

约定与 blog / knowledge 既有分页保持一致，便于前端一套逻辑通吃：

- 入参 ``offset``（>=0）与 ``limit``（默认 20，可配上限）走查询串；
- 出参 ``Page`` 带 ``total`` / ``offset`` / ``limit`` / ``items``。

用法：路由把 :data:`DefaultPage`（或 :func:`page_params` 造的自定义区间）作为依赖
注入，拿到已归一的偏移量传给服务层；服务层返回 ``(total, items)``，路由用
:meth:`Page.of` 组装。依赖每次请求产出一个新的不可变 ``PageParams``，绝不会
在多请求间共享可变状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Generic, TypeVar

from fastapi import Depends, Query
from pydantic import BaseModel, Field

T = TypeVar("T")


@dataclass(frozen=True)
class PageParams:
    """一次请求解析出的分页窗口（不可变，安全共享于依赖图）。"""

    offset: int = 0
    limit: int = 20


def page_params(default_limit: int = 20, max_limit: int = 100):
    """造一个分页依赖：读取 offset/limit 查询参数并按区间归一。

    返回的是「依赖函数」，用 :func:`Depends` 包进 ``Annotated``。不同接口对
    「一页多少条」的合理上限不一样（如导出类可到 500），故做成工厂而非固定别名。
    """

    def dep(
        offset: Annotated[int, Query(ge=0, description="跳过前 N 条")] = 0,
        limit: Annotated[
            int | None, Query(ge=1, description="本页最多多少条")
        ] = None,
    ) -> PageParams:
        resolved = limit if limit is not None else default_limit
        return PageParams(offset=offset, limit=min(resolved, max_limit))

    return dep


# 常用形态：默认 20、上限 100。特殊接口用 page_params(default, max) 再包一个。
DefaultPage = Annotated[PageParams, Depends(page_params())]


class Page(BaseModel, Generic[T]):
    """统一分页响应体（外层再被信封中间件包成 {code,data,message}）。"""

    total: int
    offset: int = 0
    limit: int = 20
    items: list[T] = Field(default_factory=list)

    @staticmethod
    def of(items: list[T], total: int, params: PageParams) -> Page[T]:
        """由「当页数据 + 总数 + 分页依赖」拼一页响应。"""
        return Page[T](
            total=total,
            offset=params.offset,
            limit=params.limit,
            items=items,
        )
