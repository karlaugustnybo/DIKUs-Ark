"""Build serving species metadata from registered snapshots or an existing data pack."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ark_pipeline.builders.species_metadata import build_global_species
from ark_pipeline.cli.sources_acquire import load_manifest
from ark_pipeline.cli.spatial_pairs import resolve_input
from ark_pipeline.runtime.progress import tracked_stage
from ark_pipeline.runtime.provenance import sha256


def source_file(
    root: Path, manifest: dict, source_id: str, logical_name: str, fallback: Path
) -> Path:
    """Registered sources take precedence; stale records never fall back silently."""
    source = manifest.get("sources", {}).get(source_id)
    if source is not None:
        if source.get("validation_status") != "passed":
            raise ValueError(f"{source_id} is not validated; run just download")
        records = [item for item in source.get("files", []) if item["logical_name"] == logical_name]
        if len(records) != 1:
            raise ValueError(f"{source_id} must register exactly one {logical_name}")
        record = records[0]
        path = resolve_input(root, record["path"])
        if not path.is_file() or path.stat().st_size != record["bytes"]:
            raise ValueError(f"{source_id}/{logical_name} is missing or changed; run just download")
        if sha256(path) != record["sha256"]:
            raise ValueError(f"{source_id}/{logical_name} checksum changed; run just download")
        return path
    if not fallback.is_file():
        raise ValueError(f"missing {source_id}/{logical_name}; run just download")
    return fallback


def optional_file(variable: str, fallback: Path) -> Path | None:
    configured = os.environ.get(variable)
    path = Path(configured).expanduser().resolve() if configured else fallback
    if configured and not path.is_file():
        raise ValueError(f"{variable} points to a missing file: {path}")
    return path if path.is_file() else None


def validate_crosswalk_sources(crosswalk: Path, manifest: dict) -> None:
    """A refreshed source pack must not silently use an older taxonomy join."""
    expected = {}
    for source_id, logical_name, summary_name in (
        ("iucn-red-list-tabular", "assessments.csv", "iucn_assessments"),
        ("iucn-red-list-tabular", "taxonomy.csv", "iucn_taxonomy"),
        ("goat-species", "tol_species_all_ranks.tsv", "goat_species"),
    ):
        for item in manifest.get("sources", {}).get(source_id, {}).get("files", []):
            if item["logical_name"] == logical_name:
                expected[summary_name] = item["sha256"]
    if not expected:
        return
    summary_path = crosswalk.with_name("match_summary.json")
    if not summary_path.is_file():
        raise ValueError(
            "crosswalk provenance is missing: match_summary.json; rebuild/review the crosswalk for the registered snapshots"
        )
    summary = json.loads(summary_path.read_text())
    stale = [
        name
        for name, digest in expected.items()
        if summary.get("sources", {}).get(name, {}).get("sha256") != digest
    ]
    if stale:
        raise ValueError(
            "crosswalk is stale for "
            + ", ".join(stale)
            + "; rebuild/review it before global-prepare"
        )


@tracked_stage("metadata")
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--h3-root", type=Path, required=True)
    parser.add_argument("--crosswalk", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        root = args.root.expanduser().resolve()
        manifest = load_manifest(root)
        assessments = source_file(
            root,
            manifest,
            "iucn-red-list-tabular",
            "assessments.csv",
            root / "IUCN_Red_List/assessments.csv",
        )
        goat = source_file(
            root,
            manifest,
            "goat-species",
            "tol_species_all_ranks.tsv",
            root / "TOL/tol_species_all_ranks.tsv",
        )
        ncbi_names = source_file(
            root,
            manifest,
            "ncbi-taxonomy",
            "names.dmp",
            root / "TOL/names.dmp",
        )
        ncbi_nodes = source_file(
            root,
            manifest,
            "ncbi-taxonomy",
            "nodes.dmp",
            root / "TOL/nodes.dmp",
        )
        if not args.crosswalk.is_file():
            raise ValueError(
                "reviewed IUCN/GoaT crosswalk is missing; set GLOBAL_CROSSWALK_PATH (see docs/pipeline/06_iucn_goat_global_crosswalk.md)"
            )
        validate_crosswalk_sources(args.crosswalk, manifest)
        h3 = args.h3_root / "h3_res3_species_global_merged.parquet"
        if not h3.is_file():
            raise ValueError(
                "res3 serving lists are missing; run just spatial and configure GLOBAL_H3_ROOT"
            )
        gbif = optional_file("GLOBAL_GBIF_SPECIES_PATH", root / "gbif_backbone_species.tsv")
        edge = optional_file(
            "GLOBAL_EDGE_SPECIES_PATH", root / "2024_EDGE_species_external_with_gbif.tsv"
        )
        missing = [
            name
            for name, path in (("GBIF name enrichment", gbif), ("EDGE group labels", edge))
            if path is None
        ]
        if missing:
            print("Optional inputs unavailable: " + ", ".join(missing), flush=True)
        build_global_species(
            crosswalk_path=args.crosswalk,
            assessments_path=assessments,
            goat_species_path=goat,
            ncbi_names_path=ncbi_names,
            ncbi_nodes_path=ncbi_nodes,
            gbif_backbone_path=gbif,
            edge_species_path=edge,
            h3_paths=[h3],
            output_dir=args.output_dir,
        )
        return 0
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
