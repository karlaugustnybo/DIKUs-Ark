"""Aggregate sorted numeric pairs without a partition-sized list hash table."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

LIST_SCHEMA = pa.schema(
    [("h3_cell", pa.uint64()), ("species_ids", pa.list_(pa.field("element", pa.int64())))]
)
RES3_PARENT_SQL = "((h3_index & ~((15::UBIGINT) << 52)) | ((3::UBIGINT) << 52) | (((1::UBIGINT) << 36) - 1))::UBIGINT"


def sorted_pair_lists(
    batches: Iterable[pa.RecordBatch],
    *,
    resolution: int,
    base_cell: int | None = None,
    deduplicate: bool = False,
) -> Iterable[pa.Table]:
    """Group an ordered pair stream, keeping only the final unfinished cell.

    The SQL sort orders both keys and can spill to disk. Adjacent comparisons
    remove exact duplicates when requested, or validate uniqueness of an
    already finalized relation, including across Arrow batches.
    Complete groups use Arrow list offsets over the original species buffer;
    Python objects are allocated per batch, never per relationship or cell.
    """
    pending_cell: int | None = None
    pending_values: list[pa.Array] = []
    previous: tuple[int, int] | None = None

    def finish_pending() -> pa.Table:
        values = pa.concat_arrays(pending_values)
        return pa.Table.from_arrays([
            pa.array([pending_cell], type=pa.uint64()),
            pa.ListArray.from_arrays(pa.array([0, len(values)], type=pa.int32()), values),
        ], schema=LIST_SCHEMA)

    for batch in batches:
        if not batch.num_rows:
            continue
        cells_array = batch.column("h3_index")
        species_array = batch.column("iucn_sis_id")
        if cells_array.null_count or species_array.null_count:
            raise ValueError("invalid pair rows: null H3 or species ID")
        cells = cells_array.to_numpy()
        species = species_array.to_numpy()
        if np.any(((cells >> 52) & 15) != resolution):
            raise ValueError("invalid pair rows: wrong H3 resolution")
        bases = (cells >> 45) & 127
        if np.any(bases > 121) or (base_cell is not None and np.any(bases != base_cell)):
            raise ValueError("invalid pair rows: wrong H3 base cell")
        same_cell = cells[1:] == cells[:-1]
        if np.any(cells[1:] < cells[:-1]) or np.any(same_cell & (species[1:] < species[:-1])):
            raise ValueError("pair stream is not sorted by H3 and species ID")
        first = (int(cells[0]), int(species[0]))
        if previous is not None and first < previous:
            raise ValueError("pair stream is not sorted across batches")
        duplicates = np.concatenate(([first == previous], same_cell & (species[1:] == species[:-1])))
        if np.any(duplicates) and not deduplicate:
            raise ValueError("pair uniqueness validation failed")
        previous = (int(cells[-1]), int(species[-1]))
        if deduplicate and np.any(duplicates):
            keep = pa.array(~duplicates)
            cells_array = cells_array.filter(keep)
            species_array = species_array.filter(keep)
            if not len(cells_array):
                continue
            cells = cells_array.to_numpy()
            species = species_array.to_numpy()
            same_cell = cells[1:] == cells[:-1]

        starts = np.concatenate(([0], np.flatnonzero(~same_cell) + 1))
        begin = 0
        if pending_cell is not None:
            if pending_cell == int(cells[0]):
                end = int(starts[1]) if len(starts) > 1 else len(cells)
                pending_values.append(species_array.slice(0, end))
                begin = 1
                if len(starts) == 1:
                    continue
            yield finish_pending()
            pending_values.clear()
        # The last group may continue into the next batch. Every earlier group
        # is complete and can be emitted without assembling any Python lists.
        if begin < len(starts) - 1:
            first_offset = int(starts[begin])
            end = int(starts[-1])
            offsets = pa.array(starts[begin:] - first_offset, type=pa.int32())
            yield pa.Table.from_arrays([
                pa.array(cells[starts[begin:-1]], type=pa.uint64()),
                pa.ListArray.from_arrays(offsets, species_array.slice(first_offset, end - first_offset)),
            ], schema=LIST_SCHEMA)
        pending_cell = int(cells[-1])
        pending_values = [species_array.slice(int(starts[-1]))]
    if pending_cell is not None:
        yield finish_pending()


def aggregate_sorted_pairs(
    connection: duckdb.DuckDBPyConnection,
    source_sql: str,
    target: Path,
    *,
    resolution: int,
    base_cell: int | None = None,
    batch_pairs: int = 250_000,
    write_pairs: int = 1_000_000,
    deduplicate: bool = False,
    derive_res3: bool = False,
) -> dict[str, int]:
    """External sort followed by one bounded grouping/validation/write pass.

    ``source_sql`` is a SQL-quoted path or list of paths supplied by the caller.
    Sorting is bounded by DuckDB's configured memory/spill settings. Python
    holds a reader batch, about ``write_pairs`` output relationships, and at
    most one unfinished cell (which may exceed either batch target).
    """
    from ark_pipeline.runtime.progress import emit

    emit(phase=f"Sort res{resolution} / base {base_cell}", force=True)
    if batch_pairs < 1 or write_pairs < 1:
        raise ValueError("pair batch sizes must be positive")
    source_query = f"SELECT h3_index, iucn_sis_id FROM read_parquet({source_sql}, hive_partitioning=false)"
    if derive_res3:
        if resolution != 3:
            raise ValueError("parent derivation requires resolution 3")
        # Parent cardinality is small. Reduce it natively before sorting, rather
        # than sorting billions of repeated parent/species rows.
        source_query = f"SELECT DISTINCT {RES3_PARENT_SQL} AS h3_index, iucn_sis_id FROM ({source_query})"
    reader = connection.execute(f"""
        SELECT h3_index, iucn_sis_id
        FROM ({source_query})
        ORDER BY h3_index, iucn_sis_id
    """).to_arrow_reader(batch_pairs)
    pending: list[pa.Table] = []
    buffered = cells = relationships = input_relationships = 0

    def counted_batches() -> Iterable[pa.RecordBatch]:
        nonlocal input_relationships
        for batch in reader:
            input_relationships += batch.num_rows
            emit(phase=f"Group res{resolution} / base {base_cell}", completed=input_relationships, unit="input pairs")
            yield batch

    try:
        with reader, pq.ParquetWriter(target, LIST_SCHEMA, compression="zstd") as writer:
            for table in sorted_pair_lists(
                counted_batches(), resolution=resolution, base_cell=base_cell, deduplicate=deduplicate
            ):
                count = len(table["species_ids"].chunk(0).values)
                cells += table.num_rows
                relationships += count
                buffered += count
                pending.append(table)
                if buffered >= write_pairs:
                    writer.write_table(pa.concat_tables(pending))
                    pending.clear()
                    buffered = 0
            if pending:
                writer.write_table(pa.concat_tables(pending))
        if relationships > input_relationships or (not deduplicate and input_relationships != relationships):
            raise ValueError("pair-to-list relationship reconciliation failed")
        if pq.read_metadata(target).num_rows != cells:
            raise ValueError("pair-to-list cell reconciliation failed")
        return {"cells": cells, "relationships": relationships,
                "input_relationships": input_relationships,
                "duplicates_removed": input_relationships - relationships}
    except BaseException:
        target.unlink(missing_ok=True)
        raise
