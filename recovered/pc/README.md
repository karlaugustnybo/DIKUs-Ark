# PC recovery bundle

This directory contains source-only artifacts recovered from the prior PC
workspace on 2026-08-29. It is an archival aid, not a second production
pipeline. The current repository implementations remain authoritative.

## Contents

- `pipeline_v2/`: the staged v2 global H3 pipeline and shared configuration.
- `pipeline_v3/`: the later untracked H3 reset pipeline, benchmark kernels,
  profiling tools, and shared metric schema.
- `diagnostics/`: two source-only DuckDB diagnostics; no captured output.
- `legacy_docs/`: design notes and historical pipeline documentation.
- `environment/`: dependency and task-runner configuration from the old
  workspace.

No raw data, generated datasets, databases, Parquet/Arrow/PMTiles files,
provider archives, credentials, caches, bytecode, logs, or notebook outputs
are included. No Jupyter or marimo notebooks were present among the recoverable
source files.

## Sanitization

Machine-specific geodata paths were replaced by `ARK_GEODATA_DIR`, defaulting
to `external_data/iucn_ranges_v2` in the recovered v3 configuration. Embedded
provider record IDs used by profiling utilities were removed. To run
`pipeline_v3/scripts/kernel_ab.py`, provide comma-separated `class:id` values
through `ARK_KERNEL_TARGETS` in a private local environment.

The original source workspace was not modified during recovery.
