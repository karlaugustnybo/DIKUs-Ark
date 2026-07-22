"""Load build-pipeline Parquet artifacts into PostgreSQL."""

import asyncio
from collections.abc import Iterator
from pathlib import Path

import asyncpg
import pyarrow.parquet as pq

from backend.config import ROOT, get_settings


def rows(path: Path, columns: list[str]) -> Iterator[tuple]:
    """Yield bounded Parquet batches directly into Postgres COPY."""
    parquet = pq.ParquetFile(path)
    for batch in parquet.iter_batches(batch_size=65_536, columns=columns):
        values = [batch.column(index).to_pylist() for index in range(len(columns))]
        yield from zip(*values, strict=True)


async def load() -> None:
    settings = get_settings()
    connection = await asyncpg.connect(settings.database_url)
    try:
        await connection.execute((ROOT / "backend" / "schema.sql").read_text())
        async with connection.transaction():
            await connection.execute("TRUNCATE cell_species, species_systems, species, app_stats CASCADE")
            species_columns = [
                "gbif_accepted_id", "species_name", "family", "redlist_category",
                "has_dna_species_level", "genus_has_dna", "family_has_dna",
                "edge_group_name", "meets_ebp",
            ]
            await connection.copy_records_to_table("species", records=rows(settings.export_dir / "species.parquet", species_columns), columns=species_columns)
            system_columns = ["gbif_accepted_id", "system"]
            await connection.copy_records_to_table(
                "species_systems",
                records=rows(settings.export_dir / "species_systems.parquet", system_columns),
                columns=system_columns,
            )
            cell_columns = ["h3_index", "resolution", "gbif_accepted_id"]
            await connection.copy_records_to_table(
                "cell_species",
                records=rows(settings.export_dir / "cell_species.parquet", cell_columns),
                columns=cell_columns,
            )
            await connection.execute(
                """
                INSERT INTO app_stats (
                    singleton, total, critically_endangered, edge_species,
                    needs_dna_sampling, res3_cells, res7_cells
                )
                SELECT TRUE,
                    COUNT(*)::int,
                    COUNT(*) FILTER (WHERE redlist_category = 'Critically Endangered')::int,
                    COUNT(*) FILTER (WHERE edge_group_name IS NOT NULL)::int,
                    COUNT(*) FILTER (WHERE has_dna_species_level = false)::int,
                    (SELECT COUNT(DISTINCT h3_index)::int FROM cell_species WHERE resolution = 3),
                    (SELECT COUNT(DISTINCT h3_index)::int FROM cell_species WHERE resolution = 7)
                FROM species
                """
            )
            await connection.execute("ANALYZE")
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(load())
