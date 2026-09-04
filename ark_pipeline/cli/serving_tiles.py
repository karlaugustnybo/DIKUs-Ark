"""Build verified, resumable PMTiles generations from prepared map metrics."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import time
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pyarrow.parquet as pq

from ark_pipeline.builders.coarse_cache import sql_path, wide_aggregate_query
from ark_pipeline.builders.fine_metrics import (
    aggregate_input_identity,
    aggregate_receipt_matches,
    input_parts,
    write_preview_metadata,
)
from ark_pipeline.runtime.progress import emit, tracked_stage
from ark_pipeline.runtime.progress import enabled as progress_enabled
from ark_pipeline.runtime.provenance import (
    atomic_json,
    code_fingerprint,
    dependency_identity,
    identity_digest,
    sha256,
)
from ark_pipeline.runtime.resources import configure_duckdb, configured_count, positive_int
from ark_pipeline.tiles import METRIC_COLUMNS, BoundaryBatchIndex, stream_query
from backend.config import get_settings

ROOT = Path(__file__).resolve().parents[2]
BOUNDARIES = {
    "admin0": ROOT / "data/boundaries/country-scope.geojson",
    "admin1": ROOT / "app/static/data/boundaries/admin1.geojson",
    "municipality": get_settings().municipality_boundaries_path,
    "eez": ROOT / "data/boundaries/eez.geojson",
    "conservation_framework": ROOT / "app/static/data/boundaries/conservation-framework.geojson",
}


def file_record(path: Path) -> dict:
    path = path.resolve(strict=True)
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": sha256(path)}


def verify_record(record: dict) -> Path:
    path = Path(record["path"])
    if not path.is_file() or path.stat().st_size != record["bytes"] or sha256(path) != record["sha256"]:
        raise ValueError(f"Prepared input changed: {path}; rerun just data-prepare")
    return path


@tracked_stage("prepared_inputs")
def record_prepared(args: argparse.Namespace) -> dict:
    """Capture only a complete, receipt-verified coarse/fine preparation."""
    h3_root = args.h3_root.resolve(strict=True)
    sources = input_parts(h3_root / "res7_merged_parts")
    if not sources:
        raise ValueError("No resolution-7 source partitions")
    if set(sources) != set(input_parts(args.parts_dir)):
        raise ValueError("Prepared metrics must cover exactly the source base cells")
    coarse_source = h3_root / "h3_res3_species_global_merged.parquet"
    with duckdb.connect(str(args.build_duckdb), read_only=True) as connection:
        configure_duckdb(connection)
        mismatch = connection.execute("""
            WITH expected AS (
                SELECT lower(to_hex(h3_cell)) AS h3_index, len(species_ids) AS total_species
                FROM read_parquet(?)
            ), actual AS (SELECT h3_index, total_species FROM h3_res3_agg_all)
            SELECT count(*) FROM (
                (SELECT * FROM expected EXCEPT ALL SELECT * FROM actual)
                UNION ALL (SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)
            )
        """, [str(coarse_source)]).fetchone()[0]
        if mismatch:
            raise ValueError("Coarse metrics do not match this source generation; rerun just data-prepare")
    metric_args = SimpleNamespace(species=args.species, species_systems=args.species_systems)
    parts = {}
    for base, source in sorted(sources.items()):
        target = args.parts_dir / source.name
        if not aggregate_receipt_matches(metric_args, source, target):
            raise ValueError(f"Stale or unverified metrics: {target}; rerun just data-prepare")
        receipt = json.loads(target.with_suffix(".receipt.json").read_text())
        output = receipt["outputs"]["aggregate"]
        parts[str(base)] = {
            "source": {"path": str(source.resolve()), "bytes": source.stat().st_size,
                       "sha256": aggregate_input_identity(metric_args, source)["source_sha256"]},
            "aggregate": {"path": str(target.resolve()), "bytes": output["bytes"], "sha256": output["sha256"]},
            "receipt": file_record(target.with_suffix(".receipt.json")),
        }
    report = {
        "status": "passed", "schema_version": 1, "h3_root": str(h3_root),
        "parts_dir": str(args.parts_dir.resolve()), "parts": parts,
        "metric_identity": metric_args._aggregate_metadata_identity,
        "files": {name: file_record(path) for name, path in {
            "build_duckdb": args.build_duckdb, "metadata": args.metadata_template,
            "species": args.species, "species_systems": args.species_systems,
            "coarse_source": coarse_source,
        }.items()},
    }
    atomic_json(args.output, report)
    return report


def read_prepared(path: Path) -> dict:
    report = json.loads(path.read_text())
    if report.get("status") != "passed" or report.get("schema_version") != 1:
        raise ValueError("Prepared-input report is incomplete; rerun just data-prepare")
    for record in report["files"].values():
        verify_record(record)
    sources = input_parts(Path(report["h3_root"]) / "res7_merged_parts")
    if {str(base) for base in sources} != set(report["parts"]):
        raise ValueError("Source partition coverage changed; rerun just data-prepare")
    if set(input_parts(Path(report["parts_dir"]))) != set(sources):
        raise ValueError("Metric partition coverage changed; rerun just data-prepare")
    metric_args = SimpleNamespace(
        species=Path(report["files"]["species"]["path"]),
        species_systems=Path(report["files"]["species_systems"]["path"]),
    )
    current = aggregate_input_identity(metric_args, next(iter(sources.values())))["metadata"]
    if current != report["metric_identity"]:
        raise ValueError("Metric code or dependencies changed; rerun just data-prepare")
    for base, records in report["parts"].items():
        if Path(records["source"]["path"]) != sources[int(base)].resolve():
            raise ValueError("Prepared source paths no longer match the generation")
        for record in records.values():
            verify_record(record)
    return report


def tool_record(executable: str) -> dict:
    path = shutil.which(executable)
    if path is None:
        raise ValueError(f"Required executable is unavailable: {executable}")
    return file_record(Path(path))


def valid_archive(path: Path) -> bool:
    # Tippecanoe/tile-join validate the tile directories while writing/merging.
    # This is a format/truncation guard, not an exhaustive decoder audit.
    try:
        with path.open("rb") as stream:
            return path.stat().st_size > 127 and stream.read(8) == b"PMTiles\x03"
    except OSError:
        return False


def reusable(directory: Path, identity: dict, names: tuple[str, ...]) -> bool:
    try:
        receipt = json.loads((directory / "receipt.json").read_text())
        if receipt.get("status") != "passed" or receipt["identity"] != identity:
            return False
        for name in names:
            path = directory / name
            record = receipt["outputs"][name]
            if path.stat().st_size != record["bytes"] or sha256(path) != record["sha256"]:
                return False
            if name.endswith(".pmtiles") and not valid_archive(path):
                return False
        return True
    except (OSError, ValueError, KeyError):
        return False


def write_receipt(directory: Path, identity: dict, names: tuple[str, ...], **extra) -> None:
    atomic_json(directory / "receipt.json", {
        "status": "passed", "identity": identity,
        "outputs": {name: {"bytes": (directory / name).stat().st_size,
                           "sha256": sha256(directory / name)} for name in names}, **extra,
    })


def compile_queries(connection, jobs: list, target: Path, indexes: dict,
                    args: argparse.Namespace, environment: dict, progress=None) -> dict:
    from ark_pipeline.runtime.progress import relay_compiler
    target.parent.mkdir(parents=True, exist_ok=True)
    minimum = min(0 if resolution == 3 else 8 for _, resolution, _, _ in jobs)
    maximum = max(6 if resolution == 3 else 12 for _, resolution, _, _ in jobs)
    command = [args.tippecanoe, "--force", "--output", str(target),
               "--minimum-zoom", str(minimum), "--maximum-zoom", str(maximum),
               "--no-feature-limit", "--no-tile-size-limit", "--preserve-input-order",
               "--read-parallel", "--temporary-directory", str(args.scratch_dir), "--quiet"]
    started = time.perf_counter()
    if progress_enabled():
        command.remove("--quiet")
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE if progress_enabled() else None, text=True, env=environment)
    relay = relay_compiler(process.stderr) if process.stderr else None
    try:
        count = 0
        feature_total = sum(expected for _, _, _, expected in jobs)
        emit("work", task="tile-stream", phase="Stream map features", overall=True, scope="phase",
             completed=0, total=feature_total, unit="features streamed", force=True)
        for name, resolution, query, expected in jobs:
            part_started = time.perf_counter()
            def features_progress(done):
                emit(task="features", phase=f"Stream {name}", completed=done, total=expected,
                     fraction=done / max(1, expected), unit="partition features")
                emit("work", task="tile-stream", phase="Stream map features", overall=True, scope="phase",
                     completed=count + done, total=feature_total, unit="features streamed")

            emitted = stream_query(connection, query, process.stdin, resolution, indexes, args.batch_size, progress=features_progress)
            if emitted != expected:
                raise ValueError(f"Expected {expected} features, emitted {emitted}")
            count += emitted
            if progress:
                progress(name, {"features": emitted, "stream_with_backpressure_seconds": time.perf_counter() - part_started})
        process.stdin.close()
        emit("task_end", task="features")
        emit("phase", phase="Compile tile zooms & archive indexes", force=True)
        streamed = time.perf_counter()
        if process.wait():
            raise subprocess.CalledProcessError(process.returncode, command)
        if not valid_archive(target):
            raise ValueError(f"Invalid PMTiles output: {target}")
        return {"features": count, "seconds": time.perf_counter() - started,
                "stream_with_backpressure_seconds": streamed - started}
    except BaseException:
        process.kill()
        with contextlib.suppress(BrokenPipeError):
            process.stdin.close()
        process.wait()
        target.unlink(missing_ok=True)
        raise
    finally:
        if relay:
            relay.join(timeout=2)
            process.stderr.close()


def compile_shard(connection, query: str, resolution: int, expected: int, directory: Path,
                  indexes: dict, args: argparse.Namespace, environment: dict) -> dict:
    target = directory / "part.tmp.pmtiles"
    report = compile_queries(connection, [(directory.name, resolution, query, expected)], target,
                             indexes, args, environment)
    os.replace(target, directory / "part.pmtiles")
    return report


def publish(output: Path, generation: Path) -> None:
    link = output / "current.tmp"
    link.unlink(missing_ok=True)
    link.symlink_to(generation.relative_to(output), target_is_directory=True)
    os.replace(link, output / "current")


@tracked_stage("tiles")
def build(args: argparse.Namespace) -> dict:
    args.output_dir = args.output_dir.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    # One writer per publication root, released by the OS after a crash.
    with (args.output_dir / ".build.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another tile build is using this output directory") from exc
        return build_locked(args)


def build_locked(args: argparse.Namespace) -> dict:
    if min(args.threads, args.tile_threads, args.batch_size) < 1:
        raise ValueError("Thread counts and batch size must be positive")
    started = time.perf_counter()
    prepared = read_prepared(args.prepared_inputs)
    boundary_paths = {key: getattr(args, key) for key in BOUNDARIES}
    missing = [key for key, path in boundary_paths.items() if not path.is_file()]
    if missing:
        raise ValueError("Missing boundary inputs: " + ", ".join(missing))
    common = {
        "code": code_fingerprint([Path(__file__), ROOT / "ark_pipeline/tiles.py",
                                  ROOT / "ark_pipeline/builders/coarse_cache.py", ROOT / "ark_pipeline/builders/fine_metrics.py",
                                  ROOT / "ark_pipeline/spatial/boundaries.py", ROOT / "ark_pipeline/runtime/provenance.py"]),
        "dependencies": {**dependency_identity(), "numpy": version("numpy")},
        "boundaries": {key: file_record(path) for key, path in boundary_paths.items()},
        "tippecanoe": tool_record(args.tippecanoe),
        "tile_join": tool_record(args.tile_join) if args.checkpoint_shards else None,
        "tile_threads": args.tile_threads,
    }
    identity = {"schema_version": 1, "prepared": prepared, "compiler": common,
                "checkpoint_shards": args.checkpoint_shards}
    generation = args.output_dir / "generations" / identity_digest(identity)
    outputs = ("priorities.pmtiles", "map-metadata.json")
    current = args.output_dir / "current"
    if current.is_symlink() and reusable(current.resolve(), identity, outputs):
        return {"status": "passed", "reused": True, "current": str(current),
                "seconds": time.perf_counter() - started}
    if reusable(generation, identity, outputs):
        publish(args.output_dir, generation)
        return {"status": "passed", "reused": True, "current": str(args.output_dir / "current"),
                "seconds": time.perf_counter() - started}
    if generation.exists():
        # Never mutate a published bundle, even if it was externally corrupted.
        generation = generation.with_name(generation.name + f"-{time.time_ns()}")
    generation.mkdir(parents=True)
    args.scratch_dir.mkdir(parents=True, exist_ok=True)
    indexes = {key: BoundaryBatchIndex.from_path(path) for key, path in boundary_paths.items()}
    environment = {**os.environ, "TIPPECANOE_MAX_THREADS": str(args.tile_threads)}
    report = {"status": "building", "shards": {}, "current": str(args.output_dir / "current")}
    # Detect normal concurrent rebuilds during the export without rereading all
    # source relationships after compiling. Inputs are already checksummed above.
    watched = [Path(record["path"]) for record in prepared["files"].values()]
    watched += [Path(record["path"]) for records in prepared["parts"].values() for record in records.values()]
    watched += list(boundary_paths.values())
    before = {str(path): (path.stat().st_ino, path.stat().st_size, path.stat().st_mtime_ns) for path in watched}
    connection = duckdb.connect(str(prepared["files"]["build_duckdb"]["path"]), read_only=True)
    try:
        connection.execute("SET threads=?", [args.threads])
        connection.execute("SET memory_limit=?", [args.memory_limit])
        connection.execute(f"SET temp_directory={sql_path(args.scratch_dir)}")
        projection = ", ".join(f'"{column}"' for column in METRIC_COLUMNS)
        coarse_count = connection.execute("SELECT count(*) FROM h3_res3_agg_all").fetchone()[0]
        jobs = [("coarse", 3, wide_aggregate_query(3), coarse_count,
                 prepared["files"]["build_duckdb"])]
        parts = []
        for base, records in sorted(prepared["parts"].items(), key=lambda item: int(item[0])):
            path = Path(records["aggregate"]["path"])
            parts.append(path)
            # Sort one base cell at a time. No global fine-cell sort or GeoJSON files.
            jobs.append((f"base_{base}", 7,
                         f"SELECT h3_index, {projection} FROM read_parquet({sql_path(path)}) ORDER BY h3_index",
                         pq.ParquetFile(path).metadata.num_rows, records["aggregate"]))
        if args.checkpoint_shards:
            shard_paths = []
            for name, resolution, query, count, source in jobs:
                shard_identity = {"compiler": common, "source": source, "resolution": resolution}
                directory = args.output_dir / "shards" / identity_digest(shard_identity)
                reused = reusable(directory, shard_identity, ("part.pmtiles",))
                if reused:
                    detail = json.loads((directory / "receipt.json").read_text())["profile"]
                else:
                    detail = compile_shard(connection, query, resolution, count, directory, indexes, args, environment)
                    write_receipt(directory, shard_identity, ("part.pmtiles",), profile=detail)
                report["shards"][name] = {**detail, "reused": reused}
                shard_paths.append(str(directory / "part.pmtiles"))
                atomic_json(generation / "build-report.json", report)
                print(f"{'Reused' if reused else 'Built'} {name}: {count:,} features", flush=True)
            merge_started = time.perf_counter()
            with (generation / "tile-join.log").open("w") as log:
                subprocess.run([args.tile_join, "--force", "--no-tile-size-limit", "--output",
                                str(generation / "priorities.pmtiles"), *shard_paths], check=True,
                               env=environment, stdout=log, stderr=log)
            report["merge_seconds"] = time.perf_counter() - merge_started
        else:
            def progress(name, detail):
                report["shards"][name] = {**detail, "reused": False}
                atomic_json(generation / "build-report.json", report)
                print(f"Streamed {name}: {detail['features']:,} features", flush=True)

            report["compilation"] = compile_queries(
                connection, [job[:4] for job in jobs], generation / "priorities.pmtiles",
                indexes, args, environment, progress,
            )
        if not valid_archive(generation / "priorities.pmtiles"):
            raise ValueError("Merged PMTiles archive is invalid")
        write_preview_metadata(connection=duckdb.connect(config={"threads": args.threads,
                               "memory_limit": args.memory_limit}),
                               template=Path(prepared["files"]["metadata"]["path"]),
                               target=generation / "map-metadata.json", parts=parts,
                               source_parts_dir=Path(prepared["h3_root"]) / "res7_merged_parts")
        report.update(status="passed", reused=False, seconds=time.perf_counter() - started,
                      features=sum(item["features"] for item in report["shards"].values()))
        after = {str(path): (path.stat().st_ino, path.stat().st_size, path.stat().st_mtime_ns) for path in watched}
        if before != after:
            raise ValueError("An input changed during tile compilation; rerun just data-prepare")
        write_receipt(generation, identity, outputs)
        atomic_json(generation / "build-report.json", report)
        publish(args.output_dir, generation)
        return report
    except BaseException as exc:
        report.update(status="failed", error=str(exc))
        atomic_json(generation / "build-report.json", report)
        raise
    finally:
        connection.close()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record", help="Record a successful preparation; run through data-prepare.")
    for name in ("h3-root", "parts-dir", "build-duckdb", "species", "species-systems", "metadata-template", "output"):
        record.add_argument("--" + name, type=Path, required=True)
    record.set_defaults(run=record_prepared)
    tiles = commands.add_parser("build")
    for name in ("prepared-inputs", "output-dir", "scratch-dir"):
        tiles.add_argument("--" + name, type=Path, required=True)
    tiles.add_argument("--tippecanoe", default="tippecanoe")
    tiles.add_argument("--tile-join", default="tile-join")
    tiles.add_argument("--threads", type=positive_int, default=configured_count("TILE_DUCKDB_THREADS", default=1))
    tiles.add_argument("--tile-threads", type=positive_int, default=configured_count("TIPPECANOE_MAX_THREADS"))
    tiles.add_argument("--memory-limit", default="750MB")
    tiles.add_argument("--batch-size", type=int, default=2048)
    tiles.add_argument("--checkpoint-shards", action="store_true",
                       help="Cache independently compiled base cells; adds a final tile-join pass.")
    for key, path in BOUNDARIES.items():
        tiles.add_argument("--" + key.replace("_", "-"), type=Path, default=path)
    tiles.set_defaults(run=build)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        report = args.run(args)
        print(json.dumps({key: value for key, value in report.items() if key not in {"parts", "metric_identity"}}, indent=2))
        return 0
    except (OSError, ValueError, KeyError, duckdb.Error, subprocess.CalledProcessError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
