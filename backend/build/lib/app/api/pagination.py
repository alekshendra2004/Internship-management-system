"""Server-side pagination, sorting and search for collection endpoints.

Every collection returns ``{"items": [...], "total": n, "limit": l, "offset": o}``.
``limit`` is capped at ``MAX_LIMIT`` regardless of what the client asks for, sort keys
are whitelisted per endpoint, and an ``id`` tiebreaker keeps page boundaries stable.
"""
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from fastapi import Query
from pydantic import BaseModel
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.errors import field_error

DEFAULT_LIMIT = 25
MAX_LIMIT = 100
MAX_OFFSET = 100_000
MAX_QUERY_LENGTH = 100


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


class PageParams:
    def __init__(
        self,
        limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
        offset: int = Query(0, ge=0, le=MAX_OFFSET),
        sort: str | None = Query(None, max_length=40, pattern=r"^-?[a-z_]+$"),
        q: str | None = Query(None, max_length=MAX_QUERY_LENGTH),
    ) -> None:
        self.limit = limit
        self.offset = offset
        self.sort = sort
        self.q = q.strip() if q and q.strip() else None


def search_clause(term: str, columns: Sequence[Any]) -> ColumnElement[bool]:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    return or_(*(col.ilike(pattern, escape="\\") for col in columns))


def empty_page(params: PageParams) -> Page[Any]:
    return Page(items=[], total=0, limit=params.limit, offset=params.offset)


async def paginate(
    session: AsyncSession,
    stmt: Select[Any] | None,
    params: PageParams,
    *,
    sort_fields: Mapping[str, Any],
    default_sort: str,
    id_column: Any,
    search_columns: Sequence[Any] = (),
    serialize: Callable[[Any], Any] | None = None,
) -> Page[Any]:
    if stmt is None:
        return empty_page(params)
    sort_key = params.sort or default_sort
    descending = sort_key.startswith("-")
    name = sort_key.lstrip("-")
    if name not in sort_fields:
        raise field_error("sort", f"Unknown sort field '{name}'. Allowed: {', '.join(sorted(sort_fields))}")
    if params.q and search_columns:
        stmt = stmt.where(search_clause(params.q, search_columns))

    total = (
        await session.execute(select(func.count()).select_from(stmt.order_by(None).subquery()))
    ).scalar_one()

    column = sort_fields[name]
    order = column.desc().nulls_last() if descending else column.asc().nulls_last()
    tiebreak = id_column.desc() if descending else id_column.asc()
    rows = (
        await session.execute(
            stmt.order_by(None).order_by(order, tiebreak).limit(params.limit).offset(params.offset)
        )
    ).scalars().all()
    items = [serialize(r) for r in rows] if serialize else list(rows)
    return Page(items=items, total=total, limit=params.limit, offset=params.offset)
