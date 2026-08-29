"""Benchmark the real species-search handler against configured PostgreSQL data."""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time

import asyncpg
from litestar.datastructures import State

from backend.app import species_page, species_suggestions
from backend.config import get_settings

DEFAULT_QUERIES = (
    "Panthera leo",
    "Panth",
    "Pantera leo",
    "Felidae",
    "100%_[]",
    "no-such-species",
)


async def benchmark(runs: int, warmup: int, queries: tuple[str, ...]) -> dict[str, object]:
    pool = await asyncpg.create_pool(get_settings().database_url, min_size=1, max_size=1)
    state = State({"db_pool": pool})

    async def table_search(query: str) -> None:
        await species_page.fn(
            state=state,
            search=query,
            sort="priority",
            order="desc",
            page=1,
            per_page=10,
            cr=4,
            en=3,
            vu=2,
            nt=1,
            dd=2,
            lc=0.1,
            sp=2,
            gen=3,
            fam=4,
            gdd=4,
            samp=0,
            cov=0,
        )

    async def map_search(query: str) -> None:
        await species_suggestions.fn(
            state=state,
            search=query,
            limit=8,
        )

    try:
        species_count = await pool.fetchval("SELECT COUNT(*) FROM species")
        async def measure(search) -> dict[str, object]:
            for query in queries:
                for _ in range(warmup):
                    await search(query)

            timings: list[float] = []
            by_query: dict[str, list[float]] = {query: [] for query in queries}
            for _ in range(runs):
                for query in queries:
                    started = time.perf_counter()
                    await search(query)
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    timings.append(elapsed_ms)
                    by_query[query].append(elapsed_ms)

            return {
                "overall_ms": {
                    "median": round(statistics.median(timings), 3),
                    "p95": percentile(timings, 0.95),
                    "max": round(max(timings), 3),
                },
                "per_query_median_ms": {
                    query: round(statistics.median(values), 3)
                    for query, values in by_query.items()
                },
            }

        def percentile(values: list[float], percentage: float) -> float:
            ordered = sorted(values)
            index = min(len(ordered) - 1, round((len(ordered) - 1) * percentage))
            return round(ordered[index], 3)

        return {
            "species_rows": species_count,
            "runs_per_query": runs,
            "queries": list(queries),
            "table": await measure(table_search),
            "map_suggestions": await measure(map_search),
        }
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=25)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("query", nargs="*")
    args = parser.parse_args()
    if args.runs < 1 or args.warmup < 0:
        parser.error("--runs must be positive and --warmup cannot be negative")
    queries = tuple(args.query) or DEFAULT_QUERIES
    print(json.dumps(asyncio.run(benchmark(args.runs, args.warmup, queries)), indent=2))


if __name__ == "__main__":
    main()
