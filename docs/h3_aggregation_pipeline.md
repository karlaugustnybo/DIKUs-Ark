# H3 Aggregation Pipeline

`app/build_h3_aggregate.py` builds global H3 cell → species aggregations from
raw `(h3_cell, res, id_no)` observation pairs.

## Overview

The pipeline takes ~22B observation triples across 7 parquet files (~66 GB)
and produces four final parquet files:

| File | Resolution | Description |
|------|-----------|-------------|
| `h3_res3_species_global.parquet` | res=3 | Unique res3 cells with species lists (observed only) |
| `h3_res7_species_global.parquet` | res=7 | Unique res7 cells with species lists (observed only) |
| `h3_res7_species_global_merged.parquet` | res=7 | Merged: observed res7 + res3 species expanded to res7 children |
| `h3_res3_species_global_merged.parquet` | res=3 | Res3 rolled up from the merged res7 data |

### Why this pipeline exists

A naive `GROUP BY h3_cell, list(DISTINCT id_no)` on 22B rows blows up RAM
and causes massive disk spill. The pipeline avoids this by:

1. **Integer encoding** — `id_no` is already an integer, `h3_cell` is already
   a `UBIGINT`. No string dictionary needed.
2. **Base-cell partitioning** — res=7 data is split into 122 buckets by H3
   base cell (extracted via bit ops, no h3 extension needed). Each bucket
   fits in RAM.
3. **Two-stage GROUP BY** — first deduplicate `(h3_cell, id_no)`, then
   aggregate into `list(id_no)`. Avoids `list(DISTINCT)` blowup.

### Data semantics

- **res=3 cells** contain species present **throughout** the entire cell area.
- **res=7 cells** contain species observed at a specific point (border cells).

This distinction drives the merge/rollup steps: res3 species are expanded to
all res7 children, and the merged res7 data is rolled back up to res3 to
produce a combined res3 view.

## Usage

```bash
# Run the full pipeline
uv run app/build_h3_aggregate.py

# Run a single step
uv run app/build_h3_aggregate.py --step partition
uv run app/build_h3_aggregate.py --step res3
uv run app/build_h3_aggregate.py --step res7
uv run app/build_h3_aggregate.py --step combine
uv run app/build_h3_aggregate.py --step verify
uv run app/build_h3_aggregate.py --step expand
uv run app/build_h3_aggregate.py --step merge
uv run app/build_h3_aggregate.py --step combine_merged
uv run app/build_h3_aggregate.py --step rollup
uv run app/build_h3_aggregate.py --step combine_rollup

# Process a single base cell (for testing or resuming)
uv run app/build_h3_aggregate.py --step res7 --base-cell 5
uv run app/build_h3_aggregate.py --step merge --base-cell 5
uv run app/build_h3_aggregate.py --step rollup --base-cell 5
```

Steps auto-skip completed work — no `--resume` flag needed. Interrupted
parquet files (truncated footer) are detected and deleted automatically.

## Configuration

| Environment variable | Default | Purpose |
|----------------------|---------|---------|
| `H3_INPUT_DIR` | `data/h3_pairs` | Raw input Parquet files |
| `H3_ENCODED_DIR` | `data/h3_encoded` | Partitioned intermediate data |
| `H3_AGGREGATED_DIR` | `data/h3_aggregated` | Final outputs + parts |
| `DUCKDB_SCRATCH_DIR` | `data/duckdb_scratch` | DuckDB spill directory; point this to a disk with 50 GB+ free |
| `DUCKDB_MEMORY_LIMIT` | `4GB` | DuckDB memory cap |
| `DUCKDB_THREADS` | `1` | Worker threads |
| Constant | Value | Purpose |
|----------|-------|---------|
| `BASE_CELL_MASK` | `127` | 7 bits for H3 base cell (0–121) |
| `BASE_CELL_SHIFT` | `45` | Bit position of base cell in H3 index |

### Environment

- **Machine:** M1 MacBook Air, 8 GB RAM, passively cooled
- **Storage:** External exFAT T7 drive (668 GB free)
- **Constraints:** DuckDB has no h3 extension; base cell extracted via
  `(h3_cell >> 45) & 127`. exFAT creates `._*` AppleDouble files that crash
  DuckDB — all globs use `data_*.parquet` or `shard_*.parquet` to exclude
  them.

## Pipeline steps

### Step 1: Partition — `--step partition`

**Input:** `h3_pairs/*.parquet` — ~22B rows of `(h3_cell, res, id_no)`

**Output:** `h3_encoded/` — partitioned by `(res, base_cell)`

**What it does:**

Reads all raw parquet files in a single `COPY` operation, computes the H3
base cell via bit extraction, and writes the data partitioned by `(res,
base_cell)`:

```sql
COPY (
    SELECT
        h3_cell,
        id_no,
        res,
        (h3_cell >> 45) & 127 AS base_cell
    FROM read_parquet('h3_pairs/*.parquet')
    WHERE id_no IS NOT NULL
) TO 'h3_encoded/' (
    FORMAT parquet,
    PARTITION_BY (res, base_cell),
    OVERWRITE_OR_IGNORE true
);
```

This creates a directory structure like:
```
h3_encoded/
  res=3/
    base_cell=0/data_*.parquet
    base_cell=1/data_*.parquet
    ...
  res=7/
    base_cell=0/data_*.parquet
    base_cell=1/data_*.parquet
    ...
```

**Why:** Splitting by base cell means each partition is small enough to
aggregate independently in RAM. res=3 and res=7 data are separated so they
can be processed with different strategies.

**Skipped if:** `h3_encoded/res=7` and `h3_encoded/res=3` both exist.

---

### Step 2: Res3 aggregation — `--step res3`

**Input:** `h3_encoded/res=3/**/data_*.parquet`

**Output:** `h3_aggregated/h3_res3_species_global.parquet`

**What it does:**

Aggregates res=3 data in a single pass using the two-stage GROUP BY:

```sql
COPY (
    WITH pairs AS (
        SELECT h3_cell, id_no
        FROM read_parquet('h3_encoded/res=3/**/data_*.parquet')
        GROUP BY h3_cell, id_no
    )
    SELECT h3_cell, list(id_no) AS species_ids
    FROM pairs
    GROUP BY h3_cell
) TO 'h3_res3_species_global.parquet' (FORMAT parquet);
```

- Stage 1: `GROUP BY h3_cell, id_no` deduplicates observation pairs.
- Stage 2: `GROUP BY h3_cell` collects all species IDs into a list per cell.

**Why one shot:** res=3 is coarse (~39,600 cells globally), so the entire
dataset fits in RAM.

**Skipped if:** Output file exists and validates (readable parquet).

**Result:** ~39,623 res3 cells, ~8.5M species entries.

---

### Step 3: Res7 aggregation — `--step res7`

**Input:** `h3_encoded/res=7/base_cell={bc}/**/data_*.parquet`

**Output:** `h3_aggregated/res7_parts/base_{bc}.parquet` (one per base cell)

**What it does:**

Iterates over all 122 base cells. For each base cell:

- **Small cells** (< 0.5 GB, ≤ 100 files): aggregate in one shot using the
  same two-stage GROUP BY as res3.
- **Large cells**: hash-partition into 16 shards via
  `hash(h3_cell) % 16`, aggregate each shard independently, then concatenate
  the results. This prevents OOM on the 8 GB Mac.

**Why hash-partitioning:** `h3_cell % N` gives the same value for ALL cells
in a base cell (they share the high bits). `hash(h3_cell) % N` distributes
cells evenly across shards.

**Key detail:** `ORDER BY id_no` was removed from the `list()` aggregation
because it caused OOM. Species IDs within a list are not sorted — sorting
is applied later in the merge step via `list_sort()`.

**Skipped if:** `base_{bc}.parquet` exists. Interrupted files are detected
via `validate_latest_output()` and deleted for reprocessing.

**Result:** 122 base-cell files, ~95.5M unique res7 cells total.

---

### Step 4: Combine res7 — `--step combine`

**Input:** `h3_aggregated/res7_parts/base_*.parquet` (122 files)

**Output:** `h3_aggregated/h3_res7_species_global.parquet`

**What it does:**

Concatenates all base-cell parquet files into a single file, ordered by
`h3_cell`:

```sql
COPY (
    SELECT h3_cell, species_ids
    FROM read_parquet('res7_parts/base_*.parquet')
    ORDER BY h3_cell
) TO 'h3_res7_species_global.parquet' (FORMAT parquet);
```

**Why:** A single sorted file is easier to query and join downstream. The
`ORDER BY` is cosmetic — it doesn't affect correctness, only read
performance for point lookups.

**Skipped if:** Output file exists and validates.

---

### Step 5: Verify — `--step verify`

**Input:** `h3_res3_species_global.parquet`, `h3_res7_species_global.parquet`

**Output:** Log output only (no files written)

**What it does:**

For each resolution, computes summary statistics:

```sql
SELECT
    COUNT(*) AS n_cells,
    SUM(len(species_ids)) AS total_species_entries,
    MAX(len(species_ids)) AS max_species_per_cell,
    AVG(len(species_ids)) AS avg_species_per_cell
FROM read_parquet('...')
```

For res=3 only (small enough), runs a duplicate check:

```sql
SELECT COUNT(*) FROM (
    SELECT h3_cell, sid, COUNT(*) AS cnt
    FROM read_parquet('...'), unnest(species_ids) AS t(sid)
    GROUP BY h3_cell, sid
    HAVING cnt > 1
)
```

For res=7, the duplicate check is skipped — unnesting billions of species
IDs into a hash table causes OOM. The pipeline guarantees no duplicates by
construction (`GROUP BY h3_cell, id_no` before `list()`).

**Note:** This step is a sanity check, not required for correctness.

---

### Step 6: Expand res3 → res7 — `--step expand`

**Input:** `h3_res3_species_global.parquet`

**Output:** `h3_aggregated/res3_to_res7_mapping/` — partitioned by base_cell

**What it does:**

Creates a mapping table `(res3_cell, res7_child)` by expanding every res3
cell to its res7 children using the `h3` Python library:

```python
for cell_int in res3_cells:
    cell_hex = f'{cell_int:015x}'
    kids = h3.cell_to_children(cell_hex, 7)
    # each child: (parent_cell_int, child_cell_int)
```

The mapping is written to a temp parquet file in batches (200 cells → ~480K
rows), then re-partitioned by base cell using DuckDB:

```sql
COPY (
    SELECT
        res3_cell,
        res7_child,
        (res7_child >> 45) & 127 AS base_cell
    FROM read_parquet('_temp_mapping.parquet')
) TO 'res3_to_res7_mapping/' (
    FORMAT parquet,
    PARTITION_BY (base_cell),
    OVERWRITE_OR_IGNORE true
);
```

**Why partition by base cell:** res3 cells and their res7 children share the
same base cell (the base cell is encoded in the high bits of the H3 index).
This keeps all related data in the same partition for the merge step.

**Skipped if:** `res3_to_res7_mapping/` exists and contains
`data_*.parquet` files.

**Result:** ~95.1M mapping rows (39,623 res3 cells × ~2,401 children each).

---

### Step 7: Merge res7 — `--step merge`

**Input:**
- `res3_to_res7_mapping/base_cell={bc}/**/data_*.parquet`
- `h3_res3_species_global.parquet`
- `res7_parts/base_{bc}.parquet` (existing observed res7)

**Output:** `h3_aggregated/res7_merged_parts/base_{bc}.parquet`

**What it does:**

For each base cell, combines:
1. **Expanded res3 species** — JOIN mapping with res3 species lists to get
   `(res7_child, species_ids)` pairs.
2. **Existing res7 observations** — `(h3_cell, species_ids)` from the
   original res7 aggregation.

Then merges them with `flatten + list_distinct` to deduplicate species IDs
across both sources:

```sql
COPY (
    WITH expanded AS (
        SELECT m.res7_child AS h3_cell, r.species_ids
        FROM read_parquet('mapping/...') m
        JOIN read_parquet('res3...') r ON m.res3_cell = r.h3_cell
    ),
    all_rows AS (
        SELECT h3_cell, species_ids FROM expanded
        UNION ALL
        SELECT h3_cell, species_ids
        FROM read_parquet('res7_parts/base_{bc}.parquet')
    )
    SELECT
        h3_cell,
        list_sort(list_distinct(flatten(list(species_ids)))) AS species_ids
    FROM all_rows
    WHERE species_ids IS NOT NULL
    GROUP BY h3_cell
) TO 'res7_merged_parts/base_{bc}.parquet' (FORMAT parquet);
```

**What this achieves:**
- res7 cells with observations get **augmented** with res3 species.
- res7 cells without observations (gaps inside res3 areas) get **created**
  with res3 species.
- `list_sort(list_distinct(...))` ensures no duplicate species IDs and a
  consistent ordering.

**OOM protection:** All base cells are hash-partitioned into 16 shards via
`hash(h3_cell) % 16` before the `flatten(list())` aggregation, same strategy
as Step 3.

**Skipped if:** `base_{bc}.parquet` exists in `res7_merged_parts/`.

---

### Step 8: Combine merged res7 — `--step combine_merged`

**Input:** `h3_aggregated/res7_merged_parts/base_*.parquet` (122 files)

**Output:** `h3_aggregated/h3_res7_species_global_merged.parquet`

**What it does:**

Same as Step 4, but for the merged res7 parts:

```sql
COPY (
    SELECT h3_cell, species_ids
    FROM read_parquet('res7_merged_parts/base_*.parquet')
    ORDER BY h3_cell
) TO 'h3_res7_species_global_merged.parquet' (FORMAT parquet);
```

This is the final merged res7 output — observed res7 cells augmented with
res3 species, plus gap res7 cells filled from res3.

---

### Step 9: Rollup to res3 — `--step rollup`

**Input:**
- `res3_to_res7_mapping/base_cell={bc}/**/data_*.parquet`
- `res7_merged_parts/base_{bc}.parquet`

**Output:** `h3_aggregated/res3_merged_parts/base_{bc}.parquet`

**What it does:**

Aggregates the merged res7 data back up to res3 parent cells by using the
mapping in reverse — JOIN on `res7_child = h3_cell`, GROUP BY `res3_cell`:

```sql
COPY (
    SELECT
        m.res3_cell AS h3_cell,
        list_sort(list_distinct(flatten(list(r.species_ids)))) AS species_ids
    FROM read_parquet('mapping/...') m
    JOIN read_parquet('res7_merged_parts/base_{bc}.parquet') r
        ON m.res7_child = r.h3_cell
    GROUP BY m.res3_cell
) TO 'res3_merged_parts/base_{bc}.parquet' (FORMAT parquet);
```

**What this achieves:**
- Each res3 cell's species list is the union of all species found in any of
  its res7 children (both observed and inherited).
- Since res3 cells and their res7 children share the same base cell, each
  res3 cell is complete within one base cell partition — no cross-partition
  aggregation needed.

**OOM protection:** All base cells are hash-partitioned into 16 shards via
`hash(res3_cell) % 16` before the `flatten(list())` aggregation, same strategy
as Steps 3 and 7.

**Skipped if:** `base_{bc}.parquet` exists in `res3_merged_parts/`.

---

### Step 10: Combine rolled-up res3 — `--step combine_rollup`

**Input:** `h3_aggregated/res3_merged_parts/base_*.parquet` (122 files)

**Output:** `h3_aggregated/h3_res3_species_global_merged.parquet`

**What it does:**

Concatenates all rolled-up res3 parts into a single file:

```sql
COPY (
    SELECT h3_cell, species_ids
    FROM read_parquet('res3_merged_parts/base_*.parquet')
    ORDER BY h3_cell
) TO 'h3_res3_species_global_merged.parquet' (FORMAT parquet);
```

This is the final merged res3 output — res3 cells rolled up from the merged
res7 data, containing the union of all species across their res7 children.

## Final outputs

```
h3_aggregated/
  h3_res3_species_global.parquet            # observed res3
  h3_res7_species_global.parquet            # observed res7
  h3_res7_species_global_merged.parquet      # merged res7 (observed + expanded res3)
  h3_res3_species_global_merged.parquet      # res3 rolled up from merged res7
```

## Directory structure

```
h3_pairs/                    # input: raw (h3_cell, res, id_no) pairs
h3_encoded/                  # intermediate: partitioned by (res, base_cell)
  res=3/base_cell={0..121}/data_*.parquet
  res=7/base_cell={0..121}/data_*.parquet
h3_aggregated/               # outputs
  h3_res3_species_global.parquet
  h3_res7_species_global.parquet
  h3_res7_species_global_merged.parquet
  h3_res3_species_global_merged.parquet
  res7_parts/base_{0..121}.parquet          # observed res7, per base cell
  res3_to_res7_mapping/base_cell={0..121}/  # (res3_cell, res7_child) mapping
  res7_merged_parts/base_{0..121}.parquet   # merged res7, per base cell
  res3_merged_parts/base_{0..121}.parquet   # rolled-up res3, per base cell
duckdb_scratch/              # DuckDB spill directory
```

## System monitor

The script includes a real-time rich dashboard showing:

- **CPU** — per-core utilization bars (8 cores, efficiency + performance)
- **Temperature** — internal SSD via `ioreg` (no sudo), CPU/GPU die via
  `powermetrics` (needs sudo — not available on M1)
- **RAM** — used/total with color coding (green < 70%, yellow < 85%, red)
- **Disk I/O** — read/write MB/s
- **Scratch** — DuckDB spill directory size
- **Progress** — per-step progress bar with ETA
- **Log** — recent operations with timestamps

CPU/GPU temperature requires sudo. Run `sudo -v` before starting the script,
or press Ctrl+C to skip (SSD temperature still works without sudo).

## Error handling

- **Interrupted parquet files:** `validate_parquet()` checks if a parquet
  file is complete by reading its footer. Truncated files are deleted
  automatically so they get reprocessed.
- **Resume on rerun:** All steps auto-skip completed work. No `--resume`
  flag needed — just re-run the same step.
- **AppleDouble files:** exFAT drives create `._*` files that crash DuckDB's
  `read_parquet`. All globs use `data_*.parquet` or `shard_*.parquet` to
  exclude them, and `clean_apple_double()` removes any that slip through.
- **OOM protection:** Large base cells are hash-partitioned into 16 shards
  via `hash(h3_cell) % 16` before aggregation. `MEMORY_LIMIT = 4GB` leaves
  room for the OS and Python on the 8 GB Mac.
