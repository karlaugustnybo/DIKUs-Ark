"""Benchmark the acquired sources through PMTiles using one stratified polygon sample."""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import multiprocessing
import os
import shutil
import sys
import time
import uuid
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pyarrow.parquet as pq

from ark_pipeline.aggregation.species_lists import export_serving_lists
from ark_pipeline.cli.benchmark_sample import SIZE_BREAKS, _size_bin
from ark_pipeline.cli.boundaries_prepare import build as build_adm2
from ark_pipeline.cli.serving_tiles import BOUNDARIES
from ark_pipeline.cli.spatial_pairs import (
    DEFAULT_PROFILE,
    PAIR_SCHEMA,
    REPOSITORY_ROOT,
    GeometryWorkerFailure,
    _flush_pair_chunks,
    _initialize_worker,
    _polyfill_wkb,
    _process_bounded,
    _write_large_coverage,
    default_duckdb_memory_limit,
    exclusion_reason_values,
    inspect_archive,
    load_acquisition_manifest,
    spatial_files,
)
from ark_pipeline.runtime.benchmark_estimates import estimate, markdown_report
from ark_pipeline.runtime.checkpoints import (
    artifact_inventory,
    find_checkpoint,
    pipeline_code,
    read_checkpoint,
    source_identity,
    validate_inventory,
)
from ark_pipeline.runtime.dashboard import Dashboard, run_command
from ark_pipeline.runtime.forecasts import load_prior
from ark_pipeline.runtime.progress import emit
from ark_pipeline.runtime.provenance import atomic_json, iso_now, runtime_identity, sha256
from ark_pipeline.runtime.resources import positive_int, resolve_resources, worker_count
from ark_pipeline.spatial.census import census_bounds, iter_census_batches
from ark_pipeline.spatial.coverage import load_spatial_profile

DEFAULT_SAMPLE = REPOSITORY_ROOT / "data/spatial-test/benchmark-samples/iucn-polygons-stratified-1000.parquet"
RESOURCE_FLAGS = ("workers", "spatial_workers", "metric_workers", "duckdb_threads", "metric_threads", "tile_threads", "tile_duckdb_threads")
BOUNDARY_ENV = dict(zip(BOUNDARIES, ("JURISDICTIONS_PATH", "ADMIN1_BOUNDARIES_PATH", "MUNICIPALITY_BOUNDARIES_PATH", "EEZ_BOUNDARIES_PATH", "CONSERVATION_BOUNDARIES_PATH")))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", type=Path, default=Path(os.environ.get("GLOBAL_DATA_ROOT", "data/external")))
    result.add_argument("--profile", type=Path, default=Path(os.environ.get("SPATIAL_PROFILE", str(DEFAULT_PROFILE))))
    result.add_argument("--sample", type=Path, default=DEFAULT_SAMPLE)
    result.add_argument("--rebuild-sample", action="store_true", help="Build a new 1,000-polygon fixture with the current row policy, using the original stratified sampler.")
    result.add_argument("--max-per-bin", type=positive_int, help="Use at most N hash-selected polygons per size band, plus the two extreme fixtures. Default: all 1,000.")
    result.add_argument("--output-root", type=Path, help="New, nonexistent directory; defaults to ROOT/benchmarks/pipeline/TIMESTAMP-ID.")
    result.add_argument("--fresh", action="store_true", help="Start a new benchmark instead of resuming compatible interrupted work.")
    result.add_argument("--workers", type=worker_count, help="Shared parallelism, positive integer or auto (also PIPELINE_WORKERS).")
    for name in RESOURCE_FLAGS[1:]:
        result.add_argument("--" + name.replace("_", "-"), type=positive_int)
    result.add_argument("--memory-limit", default=os.environ.get("DUCKDB_MEMORY_LIMIT") or default_duckdb_memory_limit())
    for key, path in BOUNDARIES.items():
        result.add_argument("--" + key.replace("_", "-"), type=Path, default=None if key == "municipality" else path,
                            help="Existing municipality file; skips acquired ADM2 preparation." if key == "municipality" else "Existing boundary file.")
    result.add_argument("--ui", choices=("auto", "rich", "plain"), default="auto")
    result.add_argument("--benchmark-report", type=Path, help="Passed benchmark to use as an ETA prior; defaults to the latest compatible run.")
    result.add_argument("--dry-run", action="store_true", help="Show stages, isolated paths and resources without reading sources or building.")
    result.add_argument("--stage", choices=("source_scan", "pairs", "lists", "boundaries"), help=argparse.SUPPRESS)
    result.add_argument("--config", type=Path, help=argparse.SUPPRESS)
    return result


def row_policy(profile) -> dict:
    return {"presence": list(profile.presence), "origin": list(profile.origin), "seasonality": list(profile.seasonality)}


def source_scan(config: dict) -> None:
    """Full source I/O calibration and current size census, without polyfilling."""
    root, output = Path(config["root"]), Path(config["output"])
    profile = load_spatial_profile(Path(config["profile"]))
    counts = [0] * (len(SIZE_BREAKS) + 1)
    archives = []
    records = spatial_files(root, load_acquisition_manifest(root))
    all_bytes = sum(record["bytes"] for record in records)
    done_bytes = 0
    for record in records:
        started = time.perf_counter()
        before_counts = counts.copy()
        emit(phase=f"Verify {record['logical_name']}", force=True)
        checked = inspect_archive(record, deep=True)
        if checked["status"] != "ready":
            raise ValueError(f"Source failed verification: {checked}")
        verified = time.perf_counter() - started
        excluded = Counter()
        scanned = 0
        for layer in checked["layers"]:
            emit(phase=f"Read bounds {record['logical_name']} · {layer['name']}", force=True)
            layer_scanned = 0
            for batch in iter_census_batches(record["resolved_path"], layer["name"], profile.geometry_batch_rows):
                for row, wkb, envelope in batch:
                    scanned += 1
                    layer_scanned += 1
                    reason = exclusion_reason_values(row["id_no"], wkb, row["presence"], row["origin"], profile, seasonal=row["seasonal"])
                    if reason:
                        excluded[reason] += 1
                        continue
                    bounds = census_bounds(wkb, envelope)
                    if len(bounds) != 4 or not all(math.isfinite(v) for v in bounds):
                        raise ValueError(f"Cannot census empty or non-finite geometry in {record['logical_name']}")
                    counts[_size_bin(max(0, bounds[2] - bounds[0]) * max(0, bounds[3] - bounds[1]))] += 1
                emit(phase=f"Read {record['logical_name']}", completed=scanned, total=checked["features"],
                     fraction=scanned / max(1, checked["features"]), unit="archive polygons")
                emit("work", task="scan-total", phase=f"Read {record['logical_name']}", overall=True,
                     completed=done_bytes + record["bytes"] * scanned / max(1, checked["features"]),
                     total=all_bytes, unit="weighted source bytes")
            if layer_scanned != layer["features"]:
                raise ValueError(f"Census row count changed in {record['logical_name']} · {layer['name']}")
        done_bytes += record["bytes"]
        archives.append({**{key: record[key] for key in ("logical_name", "bytes", "sha256", "release")},
                         "size_bin_counts": [a - b for a, b in zip(counts, before_counts)],
                         "scanned": scanned, "excluded": dict(excluded), "verification_seconds": verified,
                         "wall_seconds": time.perf_counter() - started})
        print(f"Scanned {record['logical_name']}: {scanned} polygons in {archives[-1]['wall_seconds']:.1f}s", flush=True)
    atomic_json(output / "population.json", {"row_policy": row_policy(profile), "size_breaks": list(SIZE_BREAKS),
                                             "census_method": "native-ring-envelopes-v1",
                                             "size_bin_counts": counts, "eligible_polygons": sum(counts), "archives": archives})


def select_sample(sample: Path, root: Path, profile, max_per_bin: int | None) -> dict:
    """Select on metadata only, retaining WKB in Parquet until a worker needs it."""
    metadata = json.loads(sample.with_suffix(".json").read_text())
    if metadata.get("selection", {}).get("size_breaks") != list(SIZE_BREAKS):
        raise ValueError("Sample size bands differ from the original stratified benchmark")
    columns = [name for name in pq.read_schema(sample).names if name != "geometry_wkb"]
    rows = pq.read_table(sample, columns=columns).to_pylist()
    identities = {record["logical_name"]: record["sha256"] for record in spatial_files(root, load_acquisition_manifest(root))}
    keys = [(row["logical_name"], row["source_layer"], row["source_row"]) for row in rows]
    if len(keys) != len(set(keys)) or len({row["sample_id"] for row in rows}) != len(rows):
        raise ValueError("Benchmark fixture has duplicate polygon or sample identities")
    for row in rows:
        if identities.get(row["logical_name"]) != row["source_sha256"]:
            raise ValueError("Benchmark sample is stale for the acquired sources; rerun with --rebuild-sample")
        if row["size_bin"] != _size_bin(row["bbox_area_degrees2"]):
            raise ValueError("Benchmark fixture contains an inconsistent size band")
    forced = set()
    if rows and metadata.get("selection", {}).get("forced_extremes"):
        ordered = sorted(rows, key=lambda row: (row["bbox_area_degrees2"], row["selection_priority_hex"]))
        forced = {ordered[0]["sample_id"], ordered[-1]["sample_id"]}
    selected, excluded = [], Counter()
    band_counts = Counter()
    for row in sorted(rows, key=lambda row: (row["size_bin"], row["selection_priority_hex"])):
        reason = exclusion_reason_values(row["iucn_sis_id"], b"metadata-only", row["presence"], row["origin"], profile, seasonal=row["seasonal"])
        if reason:
            excluded[reason] += 1
            continue
        extreme = row["sample_id"] in forced
        if not extreme and max_per_bin and band_counts[row["size_bin"]] >= max_per_bin:
            continue
        selected.append({**row, "forced_extreme": extreme})
        if not extreme:
            band_counts[row["size_bin"]] += 1
    if not selected:
        raise ValueError("No benchmark polygons satisfy the current row policy")
    warning = []
    if metadata.get("row_policy") != row_policy(profile):
        warning.append("The saved fixture was selected under an older or undocumented row policy. Current exclusions are applied, but newly eligible polygons are unrepresented. Estimates are provisional; use --rebuild-sample for current selection.")
    return {"path": str(sample), "sha256": sha256(sample), "metadata_sha256": sha256(sample.with_suffix(".json")),
            "original_rows": len(rows), "selected_rows": len(selected), "excluded_by_current_policy": dict(excluded),
            "row_policy": row_policy(profile), "rows": selected, "warnings": warning}


def build_pairs(config: dict) -> None:
    output = Path(config["output"])
    selection = json.loads((output / "selection.json").read_text())
    sample = Path(selection["path"])
    if sha256(sample) != selection["sha256"]:
        raise ValueError("Benchmark fixture changed during this run")
    selected = {row["sample_id"]: row for row in selection["rows"]}
    profile = load_spatial_profile(Path(config["profile"]))
    workers = config["resources"]["spatial_workers"]
    _initialize_worker(profile)
    context = multiprocessing.get_context("spawn")
    pool = (ProcessPoolExecutor(max_workers=workers, mp_context=context,
                                initializer=_initialize_worker, initargs=(profile, context.BoundedSemaphore(workers), workers)) if workers > 1 else contextlib.nullcontext(None))
    output_pairs = output / "pairs.parquet"
    (output / "polygon-timings.jsonl").write_text("")
    observations, chunks, species_ids = [], [], []
    write_seconds = 0.0
    buffered = 0
    with pool as executor, pq.ParquetWriter(output_pairs, PAIR_SCHEMA, compression="zstd") as writer:
        for batch in pq.ParquetFile(sample).iter_batches(batch_size=profile.geometry_batch_rows):
            rows = [row for row in batch.to_pylist() if row["sample_id"] in selected]
            work = [(row["geometry_wkb"], {"id": str(row["sample_id"]), "size_bin": row["size_bin"],
                                          "forced_extreme": selected[row["sample_id"]]["forced_extreme"]}) for row in rows]
            results = _process_bounded(executor, work) if executor else ((i, _polyfill_wkb(wkb)) for i, wkb in enumerate(work))
            for index, result in results:
                row = rows[index]
                if isinstance(result, GeometryWorkerFailure):
                    raise ValueError(f"sample {row['sample_id']}: {result.message}")
                cells = result.coverage.cells
                count = int(cells.size)
                started = time.perf_counter()
                if count >= profile.pair_write_rows:
                    _flush_pair_chunks(writer, chunks, species_ids)
                    buffered = 0
                    _write_large_coverage(writer, cells, row["iucn_sis_id"], profile.pair_write_rows)
                else:
                    chunks.append(cells)
                    species_ids.append(row["iucn_sis_id"])
                    buffered += count
                    if buffered >= profile.pair_write_rows:
                        _flush_pair_chunks(writer, chunks, species_ids)
                        buffered = 0
                written_seconds = time.perf_counter() - started
                write_seconds += written_seconds
                observation = {"sample_id": row["sample_id"], "iucn_sis_id": row["iucn_sis_id"], "size_bin": row["size_bin"],
                               "forced_extreme": selected[row["sample_id"]]["forced_extreme"],
                               "simplification": result.coverage.decision_simplification_audit,
                               "kernel_seconds": result.wall_seconds, "output_pairs": count}
                observations.append(observation)
                with (output / "polygon-timings.jsonl").open("a") as log:
                    log.write(json.dumps(observation) + "\n")
                print(f"Polygon {len(observations)}/{len(selected)}: {count} pairs, {result.wall_seconds:.2f}s", flush=True)
        started = time.perf_counter()
        _flush_pair_chunks(writer, chunks, species_ids)
        written_seconds = time.perf_counter() - started
        write_seconds += written_seconds
    if len(observations) != len(selected):
        raise ValueError("Not all selected polygons were processed")
    total = sum(row["output_pairs"] for row in observations)
    if not total or pq.read_metadata(output_pairs).num_rows != total:
        raise ValueError("Benchmark pair output is empty or inconsistent")
    atomic_json(output / "pairs-report.json", {"pair_rows": total, "write_seconds": write_seconds,
                                               "observations": observations, "sha256": sha256(output_pairs)})


def build_lists(config: dict) -> None:
    output = Path(config["output"])
    pairs = output / "pairs.parquet"
    report = json.loads((output / "pairs-report.json").read_text())
    if sha256(pairs) != report["sha256"]:
        raise ValueError("Benchmark pair input changed")
    result = export_serving_lists(output / "spatial", scratch_dir=output / "scratch/lists",
                                  memory_limit=config["memory_limit"], threads=config["resources"]["duckdb_threads"],
                                  archive_inputs=[{"logical_name": "benchmark-sample", "path": pairs,
                                                   "bytes": pairs.stat().st_size, "sha256": report["sha256"], "rows": report["pair_rows"]}],
                                  archive_identity={"scope": "benchmark-only", "selection_sha256": sha256(output / "selection.json"),
                                                    "profile_sha256": config["profile_sha256"]})
    atomic_json(output / "lists-report.json", result)


def build_boundaries(config: dict) -> None:
    output = Path(config["output"])
    target = output / "static/boundary-catalogs"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPOSITORY_ROOT / "app/static/data/boundary-catalogs/admin0.json", target / "admin0.json")
    build_adm2(Path(config["root"]), output / "adm2", output / "static")


def plan(args) -> tuple[dict, list[dict], dict]:
    root = args.root.expanduser().resolve()
    output = (args.output_root or root / "benchmarks/pipeline" / (iso_now().replace(":", "-") + "-" + uuid.uuid4().hex[:8])).expanduser().resolve()
    profile_path = args.profile.expanduser().resolve()
    profile = load_spatial_profile(profile_path)
    resources = resolve_resources(**{name: getattr(args, name) for name in RESOURCE_FLAGS})
    sample = args.sample.expanduser().resolve()
    make_sample = args.rebuild_sample or not sample.is_file()
    if make_sample:
        sample = output / "input/iucn-polygons-stratified-1000.parquet"
    boundaries = {key: str((getattr(args, key) or output / "adm2/current/municipality.geojson").expanduser().resolve()) for key in BOUNDARIES}
    config = {"root": str(root), "output": str(output), "sample": str(sample), "profile": str(profile_path),
              "profile_sha256": profile.digest, "resources": resources.report(), "memory_limit": args.memory_limit,
              "max_per_bin": args.max_per_bin, "boundaries": boundaries}
    h3 = output / "spatial/serving/current"
    crosswalk = output / "crosswalk/current/iucn_goat_crosswalk.parquet"
    species, systems = output / "species/species.parquet", output / "species/species_systems.parquet"
    python = [sys.executable, "-m"]
    stages = []

    def add(name, command):
        stages.append({"name": name, "command": list(map(str, command))})

    def own(name):
        add(name, [*python, "ark_pipeline.cli.benchmark_pipeline", "--stage", name, "--config", output / "config.json"])

    if make_sample:
        add("sample_setup", [*python, "ark_pipeline.cli.benchmark_sample", "--data-root", root, "--output", sample, "--profile", profile_path])
    own("source_scan")
    add("crosswalk", [*python, "ark_pipeline.cli.crosswalk_refresh", "--root", root, "--output-root", output / "crosswalk", "--memory-limit", args.memory_limit])
    own("pairs")
    own("lists")
    if args.municipality is None:
        own("boundaries")
    add("metadata", [*python, "ark_pipeline.cli.serving_metadata", "--root", root, "--h3-root", h3, "--crosswalk", crosswalk, "--output-dir", output / "species"])
    add("coarse_db", [sys.executable, "ark_pipeline/builders/source_database.py", "--target", output / "source.duckdb", "--overwrite", "--resolutions", "3",
                      "--species-parquet", species, "--species-systems-parquet", systems, "--h3-res3", h3 / "h3_res3_species_global_merged.parquet",
                      "--crosswalk", crosswalk, "--defer-lossless-validation"])
    add("coarse_cache", [sys.executable, "ark_pipeline/builders/coarse_cache.py", "--rebuild-aggregates", "--resolutions", "3", "--skip-expanded-cell-species", "--defer-lossless-validation"])
    add("fine_metrics", [*python, "ark_pipeline.builders.fine_metrics", "aggregate", "--parts-dir", h3 / "res7_merged_parts", "--species", species,
                         "--species-systems", systems, "--output-dir", output / "metrics", "--scratch-dir", output / "scratch/metrics",
                         "--memory-limit", args.memory_limit, "--no-progress"])
    add("prepared_inputs", [*python, "ark_pipeline.cli.serving_tiles", "record", "--h3-root", h3, "--parts-dir", output / "metrics", "--build-duckdb", output / "build.duckdb",
                            "--species", species, "--species-systems", systems, "--metadata-template", output / "coarse-tiles/map-metadata.json", "--output", output / "prepared-inputs.json"])
    add("tiles", [*python, "ark_pipeline.cli.serving_tiles", "build", "--prepared-inputs", output / "prepared-inputs.json", "--output-dir", output / "tiles",
                  "--scratch-dir", output / "scratch/tiles", *[value for key, path in boundaries.items() for value in ("--" + key.replace("_", "-"), path)]])
    paths = {"DATA_DIR": output, "SOURCE_DUCKDB_PATH": output / "source.duckdb", "BUILD_DUCKDB_PATH": output / "build.duckdb",
             "EXPORT_DIR": output / "exports", "TILE_DIR": output / "coarse-tiles", "PMTILES_PATH": output / "coarse-tiles/priorities.pmtiles",
             "MAP_METADATA_PATH": output / "coarse-tiles/map-metadata.json", "VALIDATION_REPORT_PATH": output / "build-validation.json",
             "SOURCE_VALIDATION_REPORT_PATH": output / "source-validation.json", "DUCKDB_SCRATCH_DIR": output / "scratch/duckdb",
             "H3_RES3_PARQUET": h3 / "h3_res3_species_global_merged.parquet", "H3_ID_CROSSWALK_PATH": crosswalk,
             "GLOBAL_DATA_ROOT": root, "GLOBAL_H3_ROOT": h3, "GLOBAL_CROSSWALK_PATH": crosswalk, "GLOBAL_PREVIEW_ROOT": output}
    env = {**{key: str(value) for key, value in paths.items()}, **resources.environment(), "DUCKDB_MEMORY_LIMIT": args.memory_limit,
           **{BOUNDARY_ENV[key]: path for key, path in boundaries.items()}}
    return config, stages, env


def run_stage(stage: dict, output: Path, environment: dict, dashboard=None) -> dict:
    return run_command(stage, output, environment, dashboard, cwd=REPOSITORY_ROOT)


def run(args) -> dict:
    config, stages, overrides = plan(args)
    output, root = Path(config["output"]), Path(config["root"])
    report = {"schema_version": 1, "status": "planned", "created_at": iso_now(), **config,
              "planned_stages": stages, "environment": overrides, "stages": [], "warnings": []}
    if args.dry_run:
        return report
    identity = {"mode": "benchmark", "config": json.loads(json.dumps(config).replace(str(output), "<run>")),
                "sources": source_identity(root), "code": pipeline_code(REPOSITORY_ROOT),
                "external_files": {str(path): sha256(path) for path in [args.sample, args.sample.with_suffix(".json"),
                    *[getattr(args, key) for key in BOUNDARIES]] if path and path.is_file()
                    and not (args.rebuild_sample and path in {args.sample, args.sample.with_suffix(".json")})}}
    resume_output = None
    if not args.fresh:
        resume_output = (output if read_checkpoint(output, identity) else None) if args.output_root else find_checkpoint(root / "benchmarks/pipeline", identity)
    if output.exists() and resume_output is None:
        raise ValueError(f"Benchmark output already exists; choose a new --output-root: {output}")
    if resume_output:
        args = argparse.Namespace(**{**vars(args), "output_root": resume_output})
        config, stages, overrides = plan(args)
        output = resume_output
        report = json.loads((output / "benchmark-report.json").read_text())
        validate_inventory(output, report.get("artifacts", {}))
        report["stages"] = [stage for stage in report["stages"] if stage["status"] == "passed"]
        report.pop("error", None)
        report["warnings"].append("Resumed after interruption: timings include earlier attempts and exclude time paused. Partial-stage cache reuse can affect throughput; use --fresh for a clean calibration.")
    for key, path in config["boundaries"].items():
        if key != "municipality" or args.municipality is not None:
            if not Path(path).is_file():
                raise ValueError(f"Missing {key} boundary: {path}")
    if shutil.which("tippecanoe") is None:
        raise ValueError("tippecanoe is required for the full benchmark")
    manifest_data = load_acquisition_manifest(root)
    required = {"iucn-spatial", "iucn-red-list-tabular", "goat-species", "ncbi-taxonomy"}
    if args.municipality is None:
        required.add("geoboundaries-adm2")
    missing = sorted(source for source in required if manifest_data.get("sources", {}).get(source, {}).get("validation_status") != "passed")
    if missing:
        raise ValueError(f"Complete data acquisition before benchmarking; missing validated sources: {', '.join(missing)}")
    if not resume_output:
        output.mkdir(parents=True, exist_ok=False)
        atomic_json(output / "config.json", config)
    manifest = root / "acquisition/current.json"
    manifest_sha = sha256(manifest)
    report.update(status="running", acquisition_manifest_sha256=manifest_sha, runtime=runtime_identity(REPOSITORY_ROOT),
                  code_sha256=identity["code"])
    if args.municipality is not None:
        report["warnings"].append("An existing municipality boundary was supplied; acquisition and ADM2 preparation are excluded from the estimated total.")
    prior, note = load_prior(root, config["profile_sha256"], args.benchmark_report)
    dashboard = Dashboard([s["name"] for s in stages], output, config["resources"], prior=prior, note=note, ui=args.ui, identity=identity)
    report["estimate_prior"] = str(prior["path"]) if prior else None
    started = time.perf_counter()
    previous_seconds = report.get("wall_seconds", 0)

    def save():
        report["wall_seconds"] = previous_seconds + time.perf_counter() - started
        atomic_json(output / "benchmark-report.json", report)
        (output / "benchmark-report.md").write_text(markdown_report(report))

    dashboard.__enter__()
    try:
        completed = {stage["name"] for stage in report["stages"]}
        if resume_output and "selection" in report:
            selection = report["selection"]
            if sha256(Path(selection["path"])) != selection["sha256"] or sha256(Path(selection["path"]).with_suffix(".json")) != selection["metadata_sha256"]:
                raise ValueError("Benchmark fixture changed since interruption; start with --fresh")
        for stage in stages:
            if sha256(manifest) != manifest_sha or load_spatial_profile(Path(config["profile"])).digest != config["profile_sha256"]:
                raise ValueError("Acquisition manifest or spatial profile changed during benchmark")
            if stage["name"] in completed:
                continue
            if stage["name"] == "source_scan":
                selection = select_sample(Path(config["sample"]), root, load_spatial_profile(Path(config["profile"])), args.max_per_bin)
                atomic_json(output / "selection.json", selection)
                dashboard.forecast.set_selection(selection)
                report["selection"] = {key: value for key, value in selection.items() if key != "rows"}
                report["warnings"].extend(selection["warnings"])
                for warning in selection["warnings"]:
                    print(warning, flush=True)
            save()
            measured = run_stage(stage, output, {**os.environ, **overrides}, dashboard=dashboard)
            previous_attempt = dashboard.states[stage["name"]].get("previous_elapsed", 0)
            if previous_attempt:
                measured["previous_attempt_seconds"] = previous_attempt
                measured["wall_seconds"] += previous_attempt
            report["stages"].append(measured)
            if measured["status"] != "passed":
                dashboard.global_estimate = None
                raise RuntimeError(f"{stage['name']} failed; see {measured['log']}")
            # Reuse the exact offline workload model once pairs/lists exist.
            if (output / "lists-report.json").is_file():
                measured_by_name = {s["name"]: s for s in report["stages"]}
                projected_stages = []
                for planned in stages:
                    item = measured_by_name.get(planned["name"])
                    if item:
                        projected_stages.append(item)
                    elif dashboard.forecast.base.get(planned["name"]) is not None:
                        projected_stages.append({"name": planned["name"], "wall_seconds": dashboard.forecast.base[planned["name"]]})
                projection = estimate(projected_stages, json.loads((output / "population.json").read_text()),
                                      json.loads((output / "pairs-report.json").read_text()), json.loads((output / "lists-report.json").read_text()))
                if len(projected_stages) == len(stages):
                    dashboard.global_estimate = projection["total_seconds"]
                report["live_projection"] = projection
            report["artifacts"] = artifact_inventory(output)
            save()
        if sha256(manifest) != manifest_sha:
            raise ValueError("Acquisition manifest changed during benchmark")
        population = json.loads((output / "population.json").read_text())
        pairs = json.loads((output / "pairs-report.json").read_text())
        lists = json.loads((output / "lists-report.json").read_text())
        report["workload"] = {"raw_pairs": pairs["pair_rows"], "eligible_polygons": population["eligible_polygons"],
                              **{key: lists[key] for key in ("res3_cells", "res3_relationships", "res7_cells", "res7_relationships")}}
        report["estimate"] = estimate(report["stages"], population, pairs, lists)
        report["status"] = "passed"
    except (Exception, KeyboardInterrupt) as error:
        report.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed", error=str(error) or "Interrupted")
    finally:
        dashboard.status = report["status"]
        dashboard.__exit__(None, None, None)
        save()
    return report


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.stage:
        if args.config is None:
            raise ValueError("--stage requires --config")
        config = json.loads(args.config.read_text())
        {"source_scan": source_scan, "pairs": build_pairs, "lists": build_lists, "boundaries": build_boundaries}[args.stage](config)
        return 0
    try:
        report = run(args)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    if args.dry_run:
        print(json.dumps(report, indent=2))
    else:
        print(markdown_report(report))
        print(f"Report: {report['output']}/benchmark-report.md")
    return 0 if report["status"] in {"passed", "planned"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
