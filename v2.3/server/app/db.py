"""SQLAlchemy 2.0 engine + session factory + 建表。"""
from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def make_engine(db_url: str) -> Engine:
    """创建 engine。SQLite 用 check_same_thread=False 以支持多线程（FastAPI 默认）。"""
    connect_args = {}
    if db_url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
    return create_engine(db_url, connect_args=connect_args, future=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db(engine: Engine) -> None:
    """根据 ORM 模型建表（CREATE TABLE IF NOT EXISTS 语义）。"""
    from app.models import Base
    Base.metadata.create_all(bind=engine)
