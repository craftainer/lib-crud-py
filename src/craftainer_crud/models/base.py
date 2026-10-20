"""Declarative base and the FastAPI DB dependency (the session factory is injected)."""

from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Annotated

from fastapi import Depends
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from craftainer_crud.config import get_session_factory


class Base(DeclarativeBase):
    """Declarative base every SQLAlchemy model inherits from."""


class IdentifiedBase(Base):
    """Base for models with a single-column integer primary key named `id`.

    Repository/CRUD generics (see craftainer_crud.repositories and
    craftainer_crud.interfaces) are bound to this
    type so they can order/select by `id` without each model redeclaring the column.
    Also carries `created_at`/`updated_at`, server-assigned in Postgres and set by
    hand in craftainer_crud.repositories.memory.InMemoryRepository (MODE=mock has no server to
    default/update them) -- see craftainer_crud.views.base.IXDTFDatetime for how they're
    serialized.
    """

    __abstract__ = True

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


async def get_db() -> AsyncGenerator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped AsyncSession, committed on success."""
    async with get_session_factory()() as session:
        yield session
        await session.commit()


# scope="function", not the Depends() default of "request": a yield dependency's
# exit code (the commit above) runs *after* the response is sent to the client at
# request scope, so a client that acts on a write's own response -- POST a hero,
# then immediately list heroes -- can issue that next request against a database
# where the INSERT hasn't committed yet, and not see its own write. NullPool (see
# above) gives every request a brand-new connection, so there's no pool affinity
# masking it either. "function" ends the dependency after the path operation but
# before the response goes out, making the write durable by the time the client
# can possibly react to it. Prefer this alias over Depends(get_db) at call sites
# so a new resource can't reintroduce that race by accident.
DBSession = Annotated[AsyncSession, Depends(get_db, scope="function")]
