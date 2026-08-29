# Recovered PC v3 pipeline

This directory retains the useful v3 source recovered from the prior PC
workspace on 2026-08-29. It is historical implementation evidence and a
benchmarking starting point, not a production pipeline. The requirements for
its replacement are defined in `docs/data_pipeline_change_requirements.md`.

The superseded v2 pipeline, legacy environment, diagnostics, and design notes
were removed after recovery. They remain available in Git history on
`codex/pc-forensics-2026-08-29` at commit `9b4ea8e`.

## Retained contents

- `pipeline_v3/config.py`: historical paths, source filters, schemas, and
  tuning parameters.
- `pipeline_v3/scripts/polyfill.py`: recovered geometry and H3 kernels.
- `pipeline_v3/scripts/00_benchmark_polyfill.py`, `kernel_ab.py`, and
  `profile_single_worker.py`: historical performance and comparison tools.
- `pipeline_v3/scripts/01_polyfill_pairs.py`: recovered pair-generation stage.
- `pipeline_v3/scripts/02_merge_metrics.py`, `03_derive_res3_metrics.py`,
  `04_partition_species_lists.py`, and `_common_metrics.py`: recovered
  aggregation and partitioning stages.

No raw data, generated datasets, databases, Parquet/Arrow/PMTiles files,
provider archives, credentials, caches, bytecode, logs, or notebook outputs
are included.

## Sanitization

Machine-specific geodata paths were replaced by `ARK_GEODATA_DIR`, defaulting
to `external_data/iucn_ranges_v2` in the recovered v3 configuration. Embedded
provider record IDs used by profiling utilities were removed. To run
`pipeline_v3/scripts/kernel_ab.py`, provide comma-separated `class:id` values
through `ARK_KERNEL_TARGETS` in a private local environment.

The original source workspace was not modified during recovery.
