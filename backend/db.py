from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import asyncpg
from litestar import Litestar
from litestar.datastructures import State

from backend.config import ROOT, get_settings

Pool = asyncpg.Pool


@asynccontextmanager
async def database_lifespan(app: Litestar) -> AsyncGenerator[None]:
    settings = get_settings()
    pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        command_timeout=30,
    )
    await apply_schema(pool, ROOT / "backend" / "schema.sql")
    app.state.db_pool = pool
    try:
        yield
    finally:
        await pool.close()


def get_pool(state: State) -> Pool:
    return cast(Pool, state.db_pool)


async def apply_schema(pool: Pool, schema_path: Path) -> None:
    async with pool.acquire() as connection:
        await connection.execute(schema_path.read_text())
