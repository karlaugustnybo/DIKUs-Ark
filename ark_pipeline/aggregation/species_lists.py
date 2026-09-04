"""Lossless, resumable export of spatial pairs to the serving list format."""

from __future__ import annotations

import json
import os
import shutil
from importlib.metadata import version
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from ark_pipeline.aggregation.pairs import LIST_SCHEMA, aggregate_sorted_pairs
from ark_pipeline.runtime.progress import emit, monitor_query
from ark_pipeline.runtime.provenance import (
    atomic_json,
    code_fingerprint,
    dependency_identity,
    identity_digest,
    iso_now,
    receipt_is_current,
    sha256,
)

PAIR_SCHEMA = pa.schema([("h3_index", pa.uint64()), ("iucn_sis_id", pa.int64())])
RES3_FILENAME = "h3_res3_species_global_merged.parquet"


def signature(schema: pa.Schema) -> list[list[str]]:
    return [[field.name, str(field.type)] for field in schema]


def sql_path(path: Path) -> str:
    return "'" + path.resolve().as_posix().replace("'", "''") + "'"


def write_receipt(path: Path, identity: dict, outputs: dict[str, Path], totals: dict) -> None:
    atomic_json(
        path,
        {
            "status": "passed",
            "completed_at": iso_now(),
            "identity": identity,
            "totals": totals,
            "outputs": {
                name: {
                    "filename": os.path.relpath(output, path.parent),
                    "bytes": output.stat().st_size,
                    "sha256": sha256(output),
                }
                for name, output in outputs.items()
            },
        },
    )


def _publish_current(serving: Path, generation: Path) -> None:
    temporary = serving / ".current.tmp"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(generation.relative_to(serving), target_is_directory=True)
    os.replace(temporary, serving / "current")


def export_code_identity() -> dict:
    return {
        "code_sha256": code_fingerprint([
            Path(__file__), Path(__file__).resolve().parents[1] / "runtime/provenance.py",
            Path(__file__).with_name("pairs.py"),
        ]),
        "dependencies": {**dependency_identity(), "numpy": version("numpy")},
    }


def export_serving_lists(
    output_root: Path,
    *,
    scratch_dir: Path,
    memory_limit: str,
    threads: int,
    force: bool = False,
    archive_inputs: list[dict] | None = None,
    archive_identity: dict | None = None,
) -> dict[str, Any]:
    """Partition res7 once, group per base cell, then publish a complete generation.

    Each completed partition has a checksum receipt. Interrupted work resumes
    within the same generation; the previous current link stays untouched until
    every partition reconciles. No crosswalk is applied: IDs remain IUCN SIS IDs.
    """
    if threads < 1:
        raise ValueError("DuckDB threads must be at least 1")
    direct = archive_inputs is not None
    if direct:
        if not archive_inputs or not archive_identity:
            raise ValueError("direct aggregation requires verified archive inputs and their identity")
        # The command verifies hashes/schema/completeness before this call. The
        # identity stores those checksums; never infer completion from filenames.
        receipt = {"identity": archive_identity,
                   "outputs": [{k: item[k] for k in ("logical_name", "bytes", "sha256", "rows")}
                               for item in archive_inputs],
                   "totals": {"archive_pair_rows": sum(item["rows"] for item in archive_inputs)}}
        res7_source = "[" + ", ".join(sql_path(item["path"]) for item in archive_inputs) + "]"
        expected_partition_rows = receipt["totals"]["archive_pair_rows"]
        inputs = {}
    else:
        relations = output_root / "relations"
        receipt_path = relations / "receipt.json"
        if not receipt_path.is_file():
            raise ValueError("finalized relations are missing; run just data-aggregate")
        receipt = json.loads(receipt_path.read_text())
        inputs = {name: relations / f"{name}_pairs.parquet" for name in ("res3", "res7")}
        if not receipt_is_current(
            receipt_path, receipt.get("identity", {}), inputs,
            {name: signature(PAIR_SCHEMA) for name in inputs},
        ):
            raise ValueError("finalized relations are stale or corrupt; run just data-aggregate")
        res7_source = sql_path(inputs["res7"])
        expected_partition_rows = receipt["totals"]["res7_relationships"]
    identity = {
        "schema_version": 2,
        "source_mode": "archive-pairs" if direct else "finalized-relations",
        "relations": receipt["identity"],
        "inputs": receipt["outputs"],
        **export_code_identity(),
        "schema": signature(LIST_SCHEMA),
    }
    serving = output_root / "serving"
    generation = serving / "generations" / identity_digest(identity)
    generation.mkdir(parents=True, exist_ok=True)
    complete_path = generation / "receipt.json"
    if not force and complete_path.is_file():
        complete = json.loads(complete_path.read_text())
        outputs = {
            name: generation / item["filename"] for name, item in complete["outputs"].items()
        }
        if receipt_is_current(
            complete_path, identity, outputs, {name: signature(LIST_SCHEMA) for name in outputs}
        ):
            _publish_current(serving, generation)
            emit("workload", counts=complete["totals"])
            return {"status": "reused", "root": str(serving / "current"), **complete["totals"]}
        # A published generation must never be repaired in place while being read.
        if (serving / "current").resolve() == generation.resolve():
            raise ValueError(
                "published serving generation is corrupt; preserve it and rebuild with a new output root"
            )
    if force and (serving / "current").resolve() == generation.resolve():
        raise ValueError("cannot overwrite a published serving generation; use a new output root")

    scratch_dir.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    stop_query_monitor = monitor_query(connection)
    try:
        connection.execute("SET memory_limit = ?", [memory_limit])
        connection.execute("SET threads = ?", [threads])
        connection.execute("SET temp_directory = ?", [str(scratch_dir.resolve())])
        connection.execute("SET preserve_insertion_order = false")
        # A single scan partitions the huge res7 relation, avoiding 122 full scans.
        partitions = generation / "pair-partitions"
        partition_receipt = generation / "partition-receipt.json"
        partition_files = {
            str(path.relative_to(generation)): path
            for path in partitions.glob("base_cell=*/data_*.parquet")
        }
        partition_identity = {"stage": "partition", "export": identity}
        try:
            same_partition_set = set(json.loads(partition_receipt.read_text())["outputs"]) == set(partition_files)
        except (OSError, KeyError, ValueError):
            same_partition_set = False
        if (
            force
            or not partition_files
            or not same_partition_set
            or not receipt_is_current(
                partition_receipt,
                partition_identity,
                partition_files,
                {name: signature(PAIR_SCHEMA) for name in partition_files},
            )
        ):
            temporary = generation / "pair-partitions.tmp"
            if temporary.exists():
                shutil.rmtree(temporary)
            emit("message", message=f"Partition {expected_partition_rows:,} raw pairs by H3 base cell")
            connection.execute(f"""
                COPY (SELECT h3_index, iucn_sis_id,
                      ((h3_index >> 45) & 127)::INTEGER AS base_cell
                      FROM read_parquet({res7_source}))
                TO {sql_path(temporary)}
                (FORMAT PARQUET, COMPRESSION ZSTD, PARTITION_BY (base_cell))
            """)
            if partitions.exists():
                shutil.rmtree(partitions)
            temporary.rename(partitions)
            partition_files = {
                str(p.relative_to(generation)): p for p in partitions.glob("base_cell=*/data_*.parquet")
            }
            rows = sum(pq.read_metadata(p).num_rows for p in partition_files.values())
            if rows != expected_partition_rows:
                raise ValueError("res7 partition relationship reconciliation failed")
            write_receipt(
                partition_receipt, partition_identity, partition_files, {"relationships": rows}
            )

        outputs = {"res3": generation / RES3_FILENAME}
        jobs = [] if direct else [("res3", 3, None, sql_path(inputs["res3"]), outputs["res3"])]
        for directory in sorted(partitions.glob("base_cell=*")):
            base = int(directory.name.split("=")[1])
            if not 0 <= base <= 121:
                raise ValueError(f"invalid H3 base cell: {base}")
            name = f"base_{base}"
            target = generation / "res7_merged_parts" / f"{name}.parquet"
            outputs[name] = target
            source = sql_path(directory / "data_*.parquet")
            jobs.append((name, 7, base, source, target))
            if direct:
                jobs.append((f"res3_base_{base}", 3, base, source,
                             generation / "res3_parts" / f"base_{base}.parquet"))
        totals = {
            "res3_cells": 0,
            "res3_relationships": 0,
            "res7_cells": 0,
            "res7_relationships": 0,
        }
        reused = 0
        raw_rows = duplicates_removed = 0
        for position, (name, resolution, base, source, target) in enumerate(jobs):
            emit(phase=f"Partition {position + 1}/{len(jobs)} · {name}", force=True)
            part_identity = {"export": identity, "partition": name}
            part_receipt = generation / "receipts" / f"{name}.json"
            if not force and receipt_is_current(
                part_receipt, part_identity, {"lists": target}, {"lists": signature(LIST_SCHEMA)}
            ):
                counts = json.loads(part_receipt.read_text())["totals"]
                reused += 1
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(".parquet.tmp")
                counts = aggregate_sorted_pairs(
                    connection, source, temporary, resolution=resolution, base_cell=base,
                    deduplicate=direct and resolution == 7,
                    derive_res3=direct and resolution == 3,
                )
                if signature(pq.read_schema(temporary)) != signature(LIST_SCHEMA):
                    temporary.unlink(missing_ok=True)
                    raise ValueError(f"{name}: list schema validation failed")
                temporary.replace(target)
                write_receipt(part_receipt, part_identity, {"lists": target}, counts)
            totals[f"res{resolution}_cells"] += counts["cells"]
            totals[f"res{resolution}_relationships"] += counts["relationships"]
            if resolution == 7:
                raw_rows += counts["input_relationships"]
                duplicates_removed += counts["duplicates_removed"]
        if direct:
            if raw_rows != expected_partition_rows or not (
                0 < totals["res3_relationships"] <= totals["res7_relationships"] <= raw_rows
            ):
                raise ValueError("archive aggregation relationship reconciliation failed")
            totals.update(archive_pair_rows=raw_rows, exact_duplicates_removed=duplicates_removed)
            temporary = outputs["res3"].with_suffix(".parquet.tmp")
            try:
                # H3 base cells have disjoint parents: concatenation, not another
                # global species-list union. Only the small coarse result sorts.
                connection.execute(f"""
                    COPY (SELECT h3_cell, species_ids
                          FROM read_parquet({sql_path(generation / 'res3_parts/base_*.parquet')})
                          ORDER BY h3_cell) TO {sql_path(temporary)}
                    (FORMAT PARQUET, COMPRESSION ZSTD)
                """)
                if pq.read_metadata(temporary).num_rows != totals["res3_cells"]:
                    raise ValueError("coarse cell reconciliation failed")
                temporary.replace(outputs["res3"])
            finally:
                temporary.unlink(missing_ok=True)
        else:
            for resolution in (3, 7):
                if totals[f"res{resolution}_relationships"] != receipt["totals"][f"res{resolution}_relationships"]:
                    raise ValueError(f"res{resolution} serving relationship reconciliation failed")
        write_receipt(complete_path, identity, outputs, totals)
        emit("workload", counts=totals)
        _publish_current(serving, generation)
        shutil.rmtree(partitions)
        return {
            "status": "built",
            "root": str(serving / "current"),
            "reused_partitions": reused,
            **totals,
        }
    finally:
        stop_query_monitor()
        connection.close()
