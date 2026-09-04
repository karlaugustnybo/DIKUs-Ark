"""Bounded native reductions from per-cell species lists to serving metrics.

Evaluate the metric definitions once per species, then gather and sum small
integer arrays. H3 and species IDs remain integers throughout the expensive
relationship pass; only the final cell IDs become strings for the serving API.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from time import monotonic

import duckdb
import h3ronpy
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from ark_pipeline.builders.coarse_cache import METRICS, SYSTEM_PREDICATES

METRIC_NAMES = tuple(
    f"{metric}__{system.lower()}" for system in SYSTEM_PREDICATES for metric in METRICS
)
METRIC_SCHEMA = pa.schema(
    [("h3_index", pa.string()), *((name, pa.int32()) for name in METRIC_NAMES)]
)


def species_metric_lookup(
    connection: duckdb.DuckDBPyConnection,
) -> tuple[np.ndarray, tuple[np.ndarray, ...]]:
    """Reuse the canonical SQL predicates, including their NULL semantics.

    Only canonical numeric IDs can match BIGINT source lists: accepting "001"
    or "1.5" as species 1 would change the old string join's meaning. A final
    zero row represents missing metadata, which the publication audit rejects
    through its relationship-count reconciliation.
    """
    expressions = ", ".join(
        f'coalesce(({system_predicate}) AND ({metric_predicate}), false)::UTINYINT '
        f'AS "{metric}__{system.lower()}"'
        for system, system_predicate in SYSTEM_PREDICATES.items()
        for metric, metric_predicate in METRICS.items()
    )
    lookup = connection.execute(f"""
        SELECT try_cast(gbif_accepted_id AS BIGINT) AS species_id, {expressions}
        FROM species
        WHERE cast(try_cast(gbif_accepted_id AS BIGINT) AS VARCHAR) = gbif_accepted_id
        ORDER BY species_id
    """).to_arrow_table()
    keys = lookup["species_id"].combine_chunks().to_numpy()
    flags = tuple(
        np.append(lookup[name].combine_chunks().to_numpy(), np.uint8(0))
        for name in METRIC_NAMES
    )
    return keys, flags


def aggregate_species_lists(
    connection: duckdb.DuckDBPyConnection,
    source: Path,
    target: Path,
    *,
    progress: Callable[[str, float | None], None] | None = None,
    batch_cells: int = 256,
    relationship_chunk: int = 250_000,
) -> None:
    """Write metrics with working arrays bounded by a batch, not a base cell.

    No cell or relationship is expanded into Python objects. The Arrow input
    batch caps cells; unusually rich batches are split again by relationship
    count, keeping a whole cell together. Only one metric's relationship-sized
    array exists at a time. Output buffers contain at most 16,384 cell rows.
    """
    from ark_pipeline.runtime.progress import emit

    if batch_cells < 1 or relationship_chunk < 1:
        raise ValueError("batch_cells and relationship_chunk must be positive")
    keys, flags = species_metric_lookup(connection)
    padded_keys = np.append(keys, np.int64(0))
    pending: list[pa.Table] = []
    pending_rows = 0
    processed = 0
    processed_relationships = 0
    last_progress = monotonic()
    if progress is not None:
        progress("Aggregating", 0.0)
    with pq.ParquetFile(source) as parquet, pq.ParquetWriter(
        target, METRIC_SCHEMA, compression="zstd"
    ) as writer:
        for batch in parquet.iter_batches(
            batch_size=batch_cells, columns=["h3_cell", "species_ids"]
        ):
            cells = batch.column("h3_cell")
            lists = batch.column("species_ids")
            offsets = lists.offsets.to_numpy()
            if cells.null_count or lists.null_count or np.any(np.diff(offsets) == 0):
                raise RuntimeError("Resolution-7 source contains null cells, null lists or empty lists")
            start = 0
            while start < len(cells):
                # One exceptionally rich cell may exceed the target; never split it.
                end = min(
                    len(cells),
                    max(start + 1, int(np.searchsorted(
                        offsets, offsets[start] + relationship_chunk, side="right"
                    )) - 1),
                )
                values = lists.values.slice(int(offsets[start]), int(offsets[end] - offsets[start]))
                valid = values.is_valid().to_numpy(zero_copy_only=False)
                ids = pc.fill_null(values, 0).to_numpy()
                positions = np.searchsorted(keys, ids)
                valid &= (positions < len(keys)) & (padded_keys[positions] == ids)
                positions[~valid] = len(keys)
                processed_relationships += len(ids)
                starts = offsets[start:end] - offsets[start]
                columns = [pa.array(h3ronpy.cells_to_string(cells.slice(start, end - start)))]
                columns.extend(
                    pa.array(np.add.reduceat(flag[positions], starts, dtype=np.int32))
                    for flag in flags
                )
                pending.append(pa.Table.from_arrays(columns, schema=METRIC_SCHEMA))
                pending_rows += end - start
                if pending_rows >= 16_384:
                    writer.write_table(pa.concat_tables(pending))
                    pending.clear()
                    pending_rows = 0
                start = end
            processed += len(cells)
            emit(task=source.stem, phase=f"{source.stem} · {processed_relationships:,} relationships", completed=processed,
                 total=parquet.metadata.num_rows, fraction=processed / max(1, parquet.metadata.num_rows), unit="partition cells")
            if progress is not None and monotonic() - last_progress >= 0.5:
                progress("Aggregating", processed / max(1, parquet.metadata.num_rows))
                last_progress = monotonic()
        if pending:
            writer.write_table(pa.concat_tables(pending))
    if progress is not None:
        progress("Aggregating", 1.0)
