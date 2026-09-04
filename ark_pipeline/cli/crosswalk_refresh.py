"""Rebuild species matches from verified snapshots and publish a resumable generation."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

import duckdb

from ark_pipeline.cli.serving_metadata import source_file
from ark_pipeline.cli.sources_acquire import load_manifest
from ark_pipeline.cli.spatial_pairs import (
    REPOSITORY_ROOT,
    default_duckdb_memory_limit,
    default_duckdb_threads,
)
from ark_pipeline.runtime.progress import tracked_stage
from ark_pipeline.runtime.provenance import (
    atomic_json,
    code_fingerprint,
    identity_digest,
    iso_now,
    sha256,
)
from ark_pipeline.runtime.resources import configure_duckdb

INPUTS = (
    ("iucn-red-list-tabular", "taxonomy.csv", "iucn_taxonomy"),
    ("iucn-red-list-tabular", "assessments.csv", "iucn_assessments"),
    ("goat-species", "tol_species_all_ranks.tsv", "goat_species"),
    ("ncbi-taxonomy", "names.dmp", "ncbi_names"),
    ("ncbi-taxonomy", "nodes.dmp", "ncbi_nodes"),
    ("ncbi-taxonomy", "merged.dmp", "ncbi_merged"),
    ("ncbi-taxonomy", "delnodes.dmp", "ncbi_deleted"),
)
OUTPUTS = ("iucn_goat_crosswalk.parquet", "iucn_goat_crosswalk.csv", "match_summary.json",
           "unresolved_candidates.parquet", "gbif_review_input.json", "AI_REVIEW.md")


def verified_inputs(root: Path) -> tuple[dict[str, Path], dict]:
    manifest = load_manifest(root)
    paths, records = {}, {}
    for source_id, logical_name, name in INPUTS:
        if source_id not in manifest.get("sources", {}):
            raise ValueError(f"{source_id} is not registered; run just download")
        paths[name] = source_file(root, manifest, source_id, logical_name, root / "unused")
        source = manifest["sources"][source_id]
        record = next(item for item in source["files"] if item["logical_name"] == logical_name)
        records[name] = {"sha256": record["sha256"], "bytes": record["bytes"], "release": source.get("release")}
    if len({paths[name].parent for name in paths if name.startswith("ncbi_")}) != 1:
        raise ValueError("registered NCBI taxdump files must share a directory")
    return paths, {
        "schema_version": 1,
        "policy": "existing deterministic taxonomy rules; uncertain matches remain unresolved; no stale API evidence",
        "inputs": records,
        "code_sha256": code_fingerprint([Path(__file__), Path(__file__).with_name("crosswalk_match.py")]),
        "duckdb": version("duckdb"),
    }


def reusable(generation: Path, identity: dict) -> bool:
    try:
        receipt = json.loads((generation / "receipt.json").read_text())
        return (
            receipt["status"] == "passed" and receipt["identity"] == identity
            and set(receipt["outputs"]) == set(OUTPUTS)
            and all((generation / name).stat().st_size == record["bytes"]
                    and sha256(generation / name) == record["sha256"]
                    for name, record in receipt["outputs"].items())
        )
    except (OSError, KeyError, ValueError):
        return False


def validate_generation(directory: Path, identity: dict, *, threads: int | None = None) -> dict:
    summary = json.loads((directory / "match_summary.json").read_text())
    for name, record in identity["inputs"].items():
        if summary.get("sources", {}).get(name, {}).get("sha256") != record["sha256"]:
            raise ValueError(f"crosswalk source changed while matching: {name}")
    with duckdb.connect(str(directory / "matching_work.duckdb"), read_only=True) as con:
        configure_duckdb(con, threads=threads)
        crosswalk_sql = str(directory / "iucn_goat_crosswalk.parquet").replace("'", "''")
        con.execute(f"CREATE TEMP VIEW published AS SELECT * FROM read_parquet('{crosswalk_sql}')")
        total, unique, nulls = con.execute(
            "SELECT count(*), count(DISTINCT iucn_sis_id), count(*) FILTER (WHERE iucn_sis_id IS NULL) FROM published"
        ).fetchone()
        source_total, source_unique = con.execute("SELECT count(*), count(DISTINCT iucn_sis_id) FROM iucn").fetchone()
        missing = con.execute("""
            SELECT count(*) FROM (
                (SELECT iucn_sis_id FROM iucn EXCEPT SELECT iucn_sis_id FROM published)
                UNION ALL
                (SELECT iucn_sis_id FROM published EXCEPT SELECT iucn_sis_id FROM iucn)
            )
        """).fetchone()[0]
        invalid = con.execute("""
            SELECT count(*) FROM published p LEFT JOIN goat g ON g.ncbi_taxid = p.matched_ncbi_species_taxid
            WHERE p.match_status IS NULL
                OR p.match_status NOT IN ('MATCHED', 'REVIEW_UNRESOLVED', 'NO_GOAT_NCBI_CANDIDATE')
                OR (p.match_status = 'MATCHED' AND (g.ncbi_taxid IS NULL OR g.goat_taxon_rank <> 'species'))
                OR (p.match_status <> 'MATCHED' AND p.matched_ncbi_species_taxid IS NOT NULL)
        """).fetchone()[0]
        counts = dict(con.execute("SELECT match_status, count(*) FROM published GROUP BY match_status").fetchall())
    if not total or total != unique or nulls or source_total != source_unique or total != source_total or missing or invalid:
        raise ValueError("crosswalk identity/row reconciliation failed")
    if summary["counts"]["iucn_taxa"] != total or summary["counts"]["matched"] != counts.get("MATCHED", 0):
        raise ValueError("crosswalk summary does not reconcile with the output")
    return {"iucn_taxa": total, **counts}


def publish(output: Path, generation: Path) -> None:
    link = output / ".current.tmp"
    link.unlink(missing_ok=True)
    link.symlink_to(generation.relative_to(output), target_is_directory=True)
    os.replace(link, output / "current")


def refresh(root: Path, output: Path, *, memory_limit: str, threads: int) -> dict:
    paths, identity = verified_inputs(root)
    generations = output / "generations"
    generations.mkdir(parents=True, exist_ok=True)
    digest = identity_digest(identity)
    for generation in sorted(generations.glob(f"{digest}-*")):
        if reusable(generation, identity):
            publish(output, generation)
            return {"status": "reused", "crosswalk": str(generation / OUTPUTS[0]),
                    "counts": json.loads((generation / "receipt.json").read_text())["counts"]}

    # A failed build cannot overwrite a prior reviewed crosswalk or current generation.
    with tempfile.TemporaryDirectory(prefix=".building-", dir=generations) as temporary:
        build = Path(temporary)
        command = [
            sys.executable, "-m", "ark_pipeline.cli.crosswalk_match",
            "--iucn-taxonomy", str(paths["iucn_taxonomy"]),
            "--iucn-assessments", str(paths["iucn_assessments"]),
            "--goat-species", str(paths["goat_species"]),
            "--ncbi-taxdump-dir", str(paths["ncbi_names"].parent),
            "--output-dir", str(build), "--memory-limit", memory_limit, "--threads", str(threads),
        ]
        subprocess.run(command, cwd=REPOSITORY_ROOT, check=True)
        counts = validate_generation(build, identity, threads=threads)
        # Matcher checksums are taken at the end; confirm the active registration
        # still selects the same source content before publishing the generation.
        _, final_identity = verified_inputs(root)
        if final_identity != identity:
            raise ValueError("registered crosswalk inputs changed during matching; rerun the same command")
        generation = generations / f"{digest}-{build.name.removeprefix('.building-')}"
        summary_path = build / "match_summary.json"
        summary = json.loads(summary_path.read_text())
        summary["outputs"] = {name: str(generation / Path(path).name)
                              for name, path in summary["outputs"].items() if name != "work_database"}
        summary["automatic_refresh"] = True
        summary["review_note"] = "Only deterministic matches accepted; unresolved candidates retain null accepted IDs. Cached API review evidence was not reused."
        atomic_json(summary_path, summary)
        (build / "matching_work.duckdb").unlink()
        atomic_json(build / "receipt.json", {
            "status": "passed", "completed_at": iso_now(), "identity": identity, "counts": counts,
            "outputs": {name: {"bytes": (build / name).stat().st_size, "sha256": sha256(build / name)} for name in OUTPUTS},
        })
        build.rename(generation)
    publish(output, generation)
    return {"status": "built", "crosswalk": str(generation / OUTPUTS[0]), "counts": counts}


@tracked_stage("crosswalk")
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--memory-limit", default=os.environ.get("DUCKDB_MEMORY_LIMIT") or default_duckdb_memory_limit())
    parser.add_argument("--threads", type=int, default=os.environ.get("DUCKDB_THREADS") or default_duckdb_threads())
    args = parser.parse_args(argv)
    try:
        root = args.root.expanduser().resolve()
        output = (args.output_root or root / "derived/iucn-goat-crosswalk").expanduser().resolve()
        result = refresh(root, output, memory_limit=args.memory_limit, threads=args.threads)
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, duckdb.Error, subprocess.CalledProcessError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
