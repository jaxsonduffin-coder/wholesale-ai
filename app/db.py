"""Database engine, session factory, and declarative base."""
from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

_url = settings.database_url

# Railway hands out "postgres://" which SQLAlchemy 2.x rejects.
if _url.startswith("postgres://"):
    _url = _url.replace("postgres://", "postgresql://", 1)

if _url.startswith("sqlite"):
    os.makedirs("data", exist_ok=True)
    engine = create_engine(
        _url, connect_args={"check_same_thread": False}, future=True
    )
else:
    engine = create_engine(
        _url, pool_pre_ping=True, pool_size=5, max_overflow=10, future=True
    )

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401  (register mappers)
    from .migrations import ensure_schema

    Base.metadata.create_all(bind=engine)
    # create_all never alters existing tables, so bring older databases up to
    # date with the additive columns added in this version.
    ensure_schema(engine)
