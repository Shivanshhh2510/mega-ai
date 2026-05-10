from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.config import get_settings

settings = get_settings()

# Async engine for API
async_engine = create_async_engine(settings.database_url, echo=False, pool_size=10, max_overflow=20)
AsyncSessionLocal = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

# Sync engine for worker
sync_engine = create_engine(settings.database_url_sync, echo=False, pool_size=5)
SyncSessionLocal = sessionmaker(bind=sync_engine)


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
