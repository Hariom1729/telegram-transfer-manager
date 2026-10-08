"""Async Database Engine and Session Management."""

from contextlib import asynccontextmanager
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from app.config import settings


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy database models."""
    pass


# Connect arguments for SQLite vs PostgreSQL
connect_args = {}
if "sqlite" in settings.DATABASE_URL:
    connect_args["check_same_thread"] = False

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    connect_args=connect_args,
    pool_pre_ping=True,
)

async_session_maker = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Provide a transactional asynchronous session scope."""
    async with async_session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Initialize database tables automatically on startup."""
    settings.ensure_directories()
    # Import all models to ensure they are registered with Base.metadata
    import app.models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # Run auto-migration for sqlite database if columns were added
        if "sqlite" in settings.DATABASE_URL:
            try:
                res = await conn.exec_driver_sql("PRAGMA table_info(chats)")
                existing_cols = [row[1] for row in res.fetchall()]
                if existing_cols:
                    if "telegram_account_id" not in existing_cols:
                        await conn.exec_driver_sql("ALTER TABLE chats ADD COLUMN telegram_account_id INTEGER")
                    if "is_megagroup" not in existing_cols:
                        await conn.exec_driver_sql("ALTER TABLE chats ADD COLUMN is_megagroup BOOLEAN DEFAULT 0")
                    if "last_used_at" not in existing_cols:
                        await conn.exec_driver_sql("ALTER TABLE chats ADD COLUMN last_used_at DATETIME")

                res_jobs = await conn.exec_driver_sql("PRAGMA table_info(transfer_jobs)")
                existing_job_cols = [row[1] for row in res_jobs.fetchall()]
                if existing_job_cols:
                    if "source_thread_id" not in existing_job_cols:
                        await conn.exec_driver_sql("ALTER TABLE transfer_jobs ADD COLUMN source_thread_id INTEGER")
                    if "source_topic_name" not in existing_job_cols:
                        await conn.exec_driver_sql("ALTER TABLE transfer_jobs ADD COLUMN source_topic_name VARCHAR(255)")
                    if "failed_message_ids" not in existing_job_cols:
                        await conn.exec_driver_sql("ALTER TABLE transfer_jobs ADD COLUMN failed_message_ids TEXT")
                    if "specific_message_ids" not in existing_job_cols:
                        await conn.exec_driver_sql("ALTER TABLE transfer_jobs ADD COLUMN specific_message_ids TEXT")

                res_accs = await conn.exec_driver_sql("PRAGMA table_info(telegram_accounts)")
                existing_acc_cols = [row[1] for row in res_accs.fetchall()]
                if existing_acc_cols:
                    if "session_string" not in existing_acc_cols:
                        await conn.exec_driver_sql("ALTER TABLE telegram_accounts ADD COLUMN session_string TEXT")
            except Exception:
                pass



async def close_db() -> None:
    """Close the database engine."""
    await engine.dispose()

