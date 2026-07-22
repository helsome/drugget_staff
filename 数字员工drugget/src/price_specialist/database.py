from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .models import Base


def create_db_engine(database_url: str, *, echo: bool = False) -> Engine:
    kwargs = {"echo": echo, "future": True}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(database_url, **kwargs)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def init_database(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    # Local smoke runs use SQLite without Alembic. Apply this additive change
    # here as well so an existing developer database can persist storefronts.
    if engine.dialect.name == "sqlite" and "store_responsibilities" in inspect(engine).get_table_names():
        columns = {item["name"] for item in inspect(engine).get_columns("store_responsibilities")}
        additive_columns = {
            "shop_home_url": "TEXT",
            "identity_status": "VARCHAR(40) NOT NULL DEFAULT 'legacy'",
            "first_discovered_at": "DATETIME",
            "last_seen_at": "DATETIME",
            "discovery_count": "INTEGER NOT NULL DEFAULT 0",
            "identity_evidence": "JSON",
        }
        with engine.begin() as connection:
            for name, ddl in additive_columns.items():
                if name not in columns:
                    connection.execute(text(f"ALTER TABLE store_responsibilities ADD COLUMN {name} {ddl}"))
        event_columns = {item["name"] for item in inspect(engine).get_columns("price_break_events")}
        if "comparison_id" not in event_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE price_break_events ADD COLUMN comparison_id VARCHAR(36)"))
    if engine.dialect.name == "sqlite" and "control_price_versions" in inspect(engine).get_table_names():
        columns = {item["name"] for item in inspect(engine).get_columns("control_price_versions")}
        additive_columns = {
            "source_line_number": "INTEGER",
            "business_confirmed": "BOOLEAN NOT NULL DEFAULT 0",
            "confirmed_by": "VARCHAR(100)",
            "confirmed_at": "DATE",
            "approval_reference": "VARCHAR(300)",
        }
        with engine.begin() as connection:
            for name, ddl in additive_columns.items():
                if name not in columns:
                    connection.execute(text(f"ALTER TABLE control_price_versions ADD COLUMN {name} {ddl}"))


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def configured_database(settings: Settings | None = None) -> tuple[Engine, sessionmaker[Session]]:
    cfg = settings or Settings.from_env()
    engine = create_db_engine(cfg.database_url)
    return engine, make_session_factory(engine)
