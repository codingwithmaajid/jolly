"""Database engine and session helpers.

SQLite by default (./jolly.db). Point DATABASE_URL at Postgres later and
nothing else changes. Tests pass `sqlite://` for an in-memory database.
"""

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import backend.models.cases  # noqa: F401  (register tables)
import backend.models.retrieval  # noqa: F401
import backend.models.trust  # noqa: F401

DEFAULT_DATABASE_URL = "sqlite:///./jolly.db"


def make_engine(database_url: str | None):
    url = database_url or DEFAULT_DATABASE_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("sqlite"):
        kwargs: dict = {"connect_args": {"check_same_thread": False}}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
        return create_engine(url, **kwargs)
    return create_engine(url, pool_pre_ping=True)


def init_db(engine) -> None:
    SQLModel.metadata.create_all(engine)


def get_session(request: Request) -> Iterator[Session]:
    with Session(request.app.state.engine) as session:
        yield session
