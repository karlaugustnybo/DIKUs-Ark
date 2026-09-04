#!/usr/bin/env python3
"""Coordinate the complete Ark-IV source acquisition workflow.

Public sources are refreshed automatically. Restricted IUCN sources are
validated and registered when the account holder has placed authorized files
in the generated staging directories and explicitly recorded that authorization.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ark_pipeline.cli.sources_acquire import (
    DEFAULT_CONFIG,
    Catalogue,
    Source,
    atomic_json,
    files_from_inventory_directory,
    inventory_release,
    load_catalogue,
    load_manifest,
    register_manual,
    required_file_names,
    resolve_stored_path,
    source_inventory,
    update_public,
    validate_input_files,
)
from ark_pipeline.runtime.progress import tracked_stage

TRUTHY = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class SourceStatus:
    source: str
    status: str
    release: str | None = None
    detail: str | None = None


def configured_release(source: Source) -> str | None:
    return inventory_release(source_inventory(source)) or source.get("release")


def incoming_directory(root: Path, source: Source) -> Path:
    release = configured_release(source)
    if release is None:
        raise ValueError(f"{source.id} has no configured release")
    return root.resolve() / "acquisition" / "incoming" / source.id / release


def registered_errors(root: Path, source: Source, record: dict[str, Any] | None) -> list[str]:
    if record is None:
        return ["not registered"]
    files: dict[str, Path] = {}
    errors: list[str] = []
    for item in record.get("files", []):
        path = resolve_stored_path(root, item["path"])
        files[item["logical_name"]] = path
        if not path.is_file():
            errors.append(f"missing registered file: {item['path']}")
        elif path.stat().st_size != item.get("bytes"):
            errors.append(f"registered size changed: {item['path']}")
    errors.extend(validate_input_files(source, files))
    return errors


def staged_files(source: Source, directory: Path) -> dict[str, Path]:
    if source_inventory(source) is not None:
        return files_from_inventory_directory(source, directory)
    return {name: directory / name for name in required_file_names(source)}


def register_staged_source(root: Path, source: Source, directory: Path) -> None:
    release = configured_release(source)
    if release is None:
        raise ValueError(f"{source.id} has no configured release")
    inventory = source_inventory(source)
    args = argparse.Namespace(
        root=root,
        source=source.id,
        release=release,
        authorized=True,
        reference=True,
        inventory_dir=directory if inventory is not None else None,
        file=[]
        if inventory is not None
        else [f"{name}={path}" for name, path in staged_files(source, directory).items()],
    )
    with contextlib.redirect_stdout(io.StringIO()):
        register_manual(args, Catalogue(sources={source.id: source}, profiles={}))


def sync_restricted_source(
    root: Path,
    source: Source,
    *,
    authorized: bool,
) -> SourceStatus:
    release = configured_release(source)
    manifest = load_manifest(root)
    record = manifest.get("sources", {}).get(source.id)
    errors = registered_errors(root, source, record)
    if not errors and (release is None or record.get("release") == release):
        return SourceStatus(source.id, "current", record.get("release"))

    directory = incoming_directory(root, source)
    directory.mkdir(parents=True, exist_ok=True)
    files = staged_files(source, directory)
    staged_errors = validate_input_files(source, files)
    if staged_errors:
        missing = sum(not path.is_file() for path in files.values())
        detail = f"{missing} file(s) missing from {directory}"
        if not missing:
            detail = "; ".join(staged_errors)
        return SourceStatus(source.id, "action-required", release, detail)
    if not authorized:
        return SourceStatus(
            source.id,
            "authorization-required",
            release,
            "files are ready; set IUCN_DATA_AUTHORIZED=true after accepting provider terms",
        )
    register_staged_source(root, source, directory)
    return SourceStatus(source.id, "registered", release)


def run_public_updates(
    root: Path,
    catalogue: Catalogue,
    source_ids: list[str],
) -> list[SourceStatus]:
    if not source_ids:
        return []
    args = argparse.Namespace(root=root, source=source_ids, force=False, dry_run=False)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        update_public(args, catalogue)
    payload = json.loads(output.getvalue())
    return [SourceStatus(item["source"], item["status"]) for item in payload.get("status", [])]


def synchronize(
    root: Path,
    catalogue: Catalogue,
    *,
    mode: str,
    authorized: bool,
) -> tuple[list[SourceStatus], bool]:
    required = catalogue.profiles["authorized"].get("required_sources", [])
    public_ids = [
        source_id
        for source_id in required
        if catalogue.sources[source_id].get("access") not in {"manual", "browser-assisted"}
    ]
    statuses = run_public_updates(root, catalogue, public_ids)
    for source_id in required:
        source = catalogue.sources[source_id]
        if source.get("access") in {"manual", "browser-assisted"}:
            statuses.append(sync_restricted_source(root, source, authorized=authorized))

    if mode == "update" and os.environ.get("IUCN_REDLIST_KEY"):
        monitor = catalogue.sources.get("iucn-assessment-catalog")
        if monitor is not None:
            statuses.extend(run_public_updates(root, catalogue, [monitor.id]))
            monitor_record = load_manifest(root).get("sources", {}).get(monitor.id, {})
            live_release = monitor_record.get("release")
            configured_iucn_releases = {
                configured_release(catalogue.sources[source_id])
                for source_id in required
                if catalogue.sources[source_id].get("provider") == "IUCN"
            }
            configured_iucn_releases.discard(None)
            if live_release and live_release not in configured_iucn_releases:
                statuses.append(
                    SourceStatus(
                        "iucn-release",
                        "action-required",
                        str(live_release),
                        "provider release differs from the checked inventories",
                    )
                )
            elif live_release:
                statuses.append(SourceStatus("iucn-release", "current", str(live_release)))

    complete = all(
        status.status in {"current", "updated", "registered", "not-modified"} for status in statuses
    )
    return statuses, complete


def write_action_plan(
    root: Path,
    catalogue: Catalogue,
    statuses: list[SourceStatus],
    complete: bool,
) -> Path:
    by_source = {item.source: item for item in statuses}
    actions = []
    for source in catalogue.sources.values():
        status = by_source.get(source.id)
        if status is None or status.status not in {"action-required", "authorization-required"}:
            continue
        inventory = source_inventory(source)
        files = []
        for item in (inventory or {}).get("files", []):
            entry = {"filename": item.get("provider_filename", item["logical_name"])}
            if item.get("download_url"):
                entry["url"] = item["download_url"]
            elif item.get("file_id"):
                entry["url"] = "https://www.iucnredlist.org/resources/files/" + item["file_id"]
            files.append(entry)
        if not files:
            files = [{"filename": name} for name in sorted(required_file_names(source))]
        actions.append(
            {
                "source": source.id,
                "release": configured_release(source),
                "official_url": source.get("official_url"),
                "destination": str(incoming_directory(root, source)),
                "status": status.status,
                "files": files,
            }
        )
    path = root.resolve() / "acquisition" / "action-required.json"
    atomic_json(path, {"complete": complete, "actions": actions})
    return path


def print_report(
    root: Path,
    mode: str,
    statuses: list[SourceStatus],
    complete: bool,
    action_plan: Path,
) -> None:
    print(f"Ark-IV data {mode}")
    print(f"Data root: {root.resolve()}")
    print()
    for item in statuses:
        release = f" ({item.release})" if item.release else ""
        detail = f" — {item.detail}" if item.detail else ""
        print(f"  {item.status:22} {item.source}{release}{detail}")
    print()
    if complete:
        print("All required source data is present and valid.")
    else:
        print("Public downloads are complete. IUCN action is still required.")
        print("Log in at https://www.iucnredlist.org/resources/spatial-data-download")
        print(f"Exact files, URLs, and destinations: {action_plan}")
        print("Download the listed missing files into those directories, then")
        print("rerun this same command. Existing current files will not be downloaded again.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("download", "update"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    return parser


@tracked_stage("acquisition")
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    catalogue = load_catalogue(args.config)
    authorized = os.environ.get("IUCN_DATA_AUTHORIZED", "").lower() in TRUTHY
    statuses, complete = synchronize(
        args.root,
        catalogue,
        mode=args.mode,
        authorized=authorized,
    )
    action_plan = write_action_plan(args.root, catalogue, statuses, complete)
    print_report(args.root, args.mode, statuses, complete, action_plan)
    return 0 if complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
