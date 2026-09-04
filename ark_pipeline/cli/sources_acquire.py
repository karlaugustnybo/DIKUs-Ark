#!/usr/bin/env python3
"""Acquire and register versioned Ark-IV source data without publishing it.

Public sources are downloaded into immutable snapshots. Restricted sources are
downloaded through an authorized provider or browser flow and then registered
here; this command never accepts provider terms.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ark_pipeline.runtime.progress import emit

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPOSITORY_ROOT / "config" / "data_sources.toml"
MANIFEST_SCHEMA_VERSION = 1
USER_AGENT = "Ark-IV-data-acquisition/1.0"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


@dataclass(frozen=True)
class Source:
    values: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.values["id"])

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


@dataclass(frozen=True)
class Catalogue:
    sources: dict[str, Source]
    profiles: dict[str, dict[str, Any]]


def load_catalogue(path: Path) -> Catalogue:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    if raw.get("schema_version") != 1:
        raise ValueError(f"Unsupported source catalogue schema: {raw.get('schema_version')}")
    sources = {item["id"]: Source(item) for item in raw.get("sources", [])}
    if len(sources) != len(raw.get("sources", [])):
        raise ValueError("Source catalogue contains duplicate IDs")
    return Catalogue(sources=sources, profiles=raw.get("profiles", {}))


def acquisition_root(data_root: Path) -> Path:
    return data_root.resolve() / "acquisition"


def current_manifest_path(data_root: Path) -> Path:
    return acquisition_root(data_root) / "current.json"


def load_manifest(data_root: Path) -> dict[str, Any]:
    path = current_manifest_path(data_root)
    if not path.exists():
        return {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "updated_at": None,
            "sources": {},
        }
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError(f"Unsupported acquisition manifest schema in {path}")
    return value


def stored_path(data_root: Path, path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(data_root.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def resolve_stored_path(data_root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else data_root.resolve() / path


def source_columns(source: Source, logical_name: str) -> list[str]:
    for requirement in source.get("column_requirements", []):
        if requirement["file"] == logical_name:
            return list(requirement["columns"])
    return []


def source_inventory(source: Source) -> dict[str, Any] | None:
    configured = source.get("inventory_file")
    if not configured:
        return None
    path = Path(configured)
    if not path.is_absolute():
        path = REPOSITORY_ROOT / path
    with path.open("rb") as handle:
        inventory = tomllib.load(handle)
    if inventory.get("schema_version") != 1:
        raise ValueError(f"Unsupported source inventory schema in {path}")
    files = inventory.get("files", [])
    names = [item.get("logical_name") for item in files]
    if not files or any(not name for name in names) or len(set(names)) != len(names):
        raise ValueError(f"Invalid or duplicate logical file names in {path}")
    return inventory


def inventory_release(inventory: dict[str, Any] | None) -> str | None:
    if inventory is None:
        return None
    release = inventory.get("release", inventory.get("red_list_version"))
    return str(release) if release is not None else None


def required_file_names(source: Source) -> set[str]:
    required = set(source.get("required_files", []))
    inventory = source_inventory(source)
    if inventory:
        required.update(item["logical_name"] for item in inventory["files"])
    return required


def delimited_header(path: Path) -> list[str]:
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    with path.open("r", encoding="utf-8-sig", newline="", errors="strict") as handle:
        return next(csv.reader(handle, delimiter=delimiter))


def validate_input_files(source: Source, files: dict[str, Path]) -> list[str]:
    errors: list[str] = []
    required_files = required_file_names(source)
    missing = sorted(required_files - files.keys())
    if missing:
        errors.append(f"missing required file(s): {', '.join(missing)}")
    minimum = int(source.get("min_files", 0))
    if len(files) < minimum:
        errors.append(f"requires at least {minimum} file(s), received {len(files)}")
    allowed = {suffix.lower() for suffix in source.get("allowed_suffixes", [])}
    inventory = source_inventory(source)
    inventory_files = {
        item["logical_name"]: item for item in (inventory or {}).get("files", [])
    }
    archive_suffixes = {
        suffix.lower() for suffix in source.get("archive_required_suffixes", [])
    }
    for logical_name, path in files.items():
        if not path.is_file():
            errors.append(f"{logical_name}: file does not exist: {path}")
            continue
        if path.stat().st_size == 0:
            errors.append(f"{logical_name}: file is empty")
        expected_bytes = inventory_files.get(logical_name, {}).get("expected_bytes")
        if expected_bytes is not None and path.stat().st_size != int(expected_bytes):
            errors.append(
                f"{logical_name}: expected {expected_bytes} bytes, "
                f"received {path.stat().st_size}"
            )
        if allowed and path.suffix.lower() not in allowed:
            errors.append(f"{logical_name}: unsupported suffix {path.suffix!r}")
        if archive_suffixes and path.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(path) as archive:
                    members = {
                        Path(name).suffix.lower()
                        for name in archive.namelist()
                        if not name.endswith("/")
                    }
            except (OSError, zipfile.BadZipFile) as error:
                errors.append(f"{logical_name}: invalid ZIP archive: {error}")
            else:
                absent_suffixes = sorted(archive_suffixes - members)
                if absent_suffixes:
                    errors.append(
                        f"{logical_name}: archive missing member type(s): "
                        + ", ".join(absent_suffixes)
                    )
        required_columns = source_columns(source, logical_name)
        if required_columns:
            try:
                header = set(delimited_header(path))
            except (OSError, UnicodeError, StopIteration, csv.Error) as error:
                errors.append(f"{logical_name}: cannot read header: {error}")
                continue
            absent = sorted(set(required_columns) - header)
            if absent:
                errors.append(f"{logical_name}: missing column(s): {', '.join(absent)}")
    return errors


def file_record(data_root: Path, logical_name: str, path: Path) -> dict[str, Any]:
    return {
        "logical_name": logical_name,
        "path": stored_path(data_root, path),
        "bytes": path.stat().st_size,
        "sha256": sha256(path),
    }


def source_record(
    source: Source,
    release: str,
    files: list[dict[str, Any]],
    *,
    acquisition: str,
    remote: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "source_id": source.id,
        "provider": source.get("provider"),
        "release": release,
        "acquired_at": iso_now(),
        "acquisition": acquisition,
        "official_url": source.get("official_url"),
        "terms_url": source.get("terms_url"),
        "update_policy": source.get("update_policy"),
        "files": sorted(files, key=lambda item: item["logical_name"]),
        "validation_status": "passed",
    }
    if remote:
        record["remote"] = remote
    return record


def activate(data_root: Path, base: dict[str, Any], updates: dict[str, Any]) -> Path:
    manifest = dict(base)
    manifest["schema_version"] = MANIFEST_SCHEMA_VERSION
    manifest["updated_at"] = iso_now()
    manifest["sources"] = {**base.get("sources", {}), **updates}
    current = current_manifest_path(data_root)
    atomic_json(current, manifest)
    timestamp = utc_now().strftime("%Y%m%dT%H%M%S.%fZ")
    historical = acquisition_root(data_root) / "manifests" / f"{timestamp}.json"
    atomic_json(historical, manifest)
    return current


def parse_file_argument(value: str) -> tuple[str, Path]:
    if "=" in value:
        logical_name, raw_path = value.split("=", 1)
        if not logical_name:
            raise argparse.ArgumentTypeError("logical file name cannot be empty")
        return logical_name, Path(raw_path).expanduser()
    path = Path(value).expanduser()
    return path.name, path


def files_from_inventory_directory(source: Source, directory: Path) -> dict[str, Path]:
    inventory = source_inventory(source)
    if inventory is None:
        raise SystemExit(f"{source.id} has no configured download inventory")
    files: dict[str, Path] = {}
    missing_provider_names: list[str] = []
    for item in inventory["files"]:
        provider_filename = item.get("provider_filename")
        if not provider_filename:
            missing_provider_names.append(item["logical_name"])
            continue
        files[item["logical_name"]] = directory.expanduser() / provider_filename
    if missing_provider_names:
        raise SystemExit(
            "Inventory is missing provider_filename for: "
            + ", ".join(sorted(missing_provider_names))
        )
    return files


def register_manual(args: argparse.Namespace, catalogue: Catalogue) -> int:
    source = catalogue.sources.get(args.source)
    if source is None:
        raise SystemExit(f"Unknown source: {args.source}")
    if source.get("access") not in {"manual", "browser-assisted"}:
        raise SystemExit(f"{source.id} is {source.get('access')}; use update instead")
    if not args.authorized:
        raise SystemExit(
            "Registration requires --authorized to record that you obtained the files "
            "through the provider's official route and are authorized to use them."
        )
    inventory_directory = getattr(args, "inventory_dir", None)
    if inventory_directory is not None:
        files = files_from_inventory_directory(source, inventory_directory)
    else:
        parsed_files = [parse_file_argument(value) for value in args.file]
        if len({name for name, _ in parsed_files}) != len(parsed_files):
            raise SystemExit("Registration contains duplicate logical file names")
        files = dict(parsed_files)
    inventory = source_inventory(source)
    configured_release = inventory_release(inventory)
    if inventory and args.release != configured_release:
        raise SystemExit(
            f"{source.id} inventory is for release {configured_release}; "
            f"cannot register it as {args.release}"
        )
    errors = validate_input_files(source, files)
    if errors:
        raise SystemExit("Registration failed:\n  - " + "\n  - ".join(errors))

    data_root = args.root.resolve()
    records: list[dict[str, Any]] = []
    if args.reference:
        records = [file_record(data_root, name, path) for name, path in files.items()]
        acquisition = "manual-reference"
    else:
        preliminary = [(name, path, sha256(path)) for name, path in files.items()]
        combined = hashlib.sha256(
            "\n".join(f"{name}:{digest}" for name, _, digest in sorted(preliminary)).encode()
        ).hexdigest()
        snapshot = (
            acquisition_root(data_root)
            / "snapshots"
            / source.id
            / f"{args.release}--{combined[:12]}"
        )
        snapshot.mkdir(parents=True, exist_ok=True)
        for name, path, digest in preliminary:
            destination = snapshot / name
            if destination.exists() and sha256(destination) != digest:
                raise SystemExit(f"Immutable snapshot collision: {destination}")
            if not destination.exists():
                shutil.copy2(path, destination)
            records.append(
                {
                    "logical_name": name,
                    "path": stored_path(data_root, destination),
                    "bytes": destination.stat().st_size,
                    "sha256": digest,
                }
            )
        acquisition = "manual-copy"

    record = source_record(source, args.release, records, acquisition=acquisition)
    current = activate(data_root, load_manifest(data_root), {source.id: record})
    print(json.dumps({"status": "registered", "source": source.id, "manifest": str(current)}))
    return 0


def age_days(record: dict[str, Any]) -> float:
    acquired = str(record.get("acquired_at", "")).replace("Z", "+00:00")
    try:
        return (utc_now() - datetime.fromisoformat(acquired)).total_seconds() / 86400
    except ValueError:
        return float("inf")


def is_due(source: Source, record: dict[str, Any] | None, force: bool) -> bool:
    if force or record is None:
        return True
    configured_release = inventory_release(source_inventory(source))
    if configured_release is not None:
        return configured_release != str(record.get("release"))
    if source.get("update_policy") == "pinned-discontinued":
        return False
    return age_days(record) >= int(source.get("interval_days", 0))


def request_headers(previous: dict[str, Any] | None) -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/octet-stream"}
    remote = (previous or {}).get("remote", {})
    if remote.get("etag"):
        headers["If-None-Match"] = remote["etag"]
    if remote.get("last_modified"):
        headers["If-Modified-Since"] = remote["last_modified"]
    return headers


def download(
    source: Source,
    previous: dict[str, Any] | None,
    destination: Path,
    *,
    url: str | None = None,
) -> dict[str, Any] | None:
    request = urllib.request.Request(
        url or source.get("download_url"), headers=request_headers(previous), method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response, destination.open("wb") as output:
            try:
                total = max(0, int(response.headers.get("Content-Length") or 0))
            except ValueError:
                total = 0
            completed = 0
            while chunk := response.read(8 * 1024 * 1024):
                output.write(chunk)
                completed += len(chunk)
                emit(task="download", phase=f"Download {destination.name}", completed=completed, total=total,
                     fraction=min(1, completed / total) if total else None, unit="download bytes")
            emit("task_end", task="download")
            return {
                "url": response.geturl(),
                "etag": response.headers.get("ETag"),
                "last_modified": response.headers.get("Last-Modified"),
            }
    except urllib.error.HTTPError as error:
        if error.code == 304:
            return None
        raise


def safe_extract_tar(archive: Path, members: list[str], destination: Path) -> list[Path]:
    extracted: list[Path] = []
    with tarfile.open(archive, "r:*") as handle:
        available = {member.name: member for member in handle.getmembers()}
        for name in members:
            member = available.get(name)
            if member is None or not member.isfile():
                raise ValueError(f"Archive is missing regular file {name!r}")
            target = destination / name
            source_handle = handle.extractfile(member)
            if source_handle is None:
                raise ValueError(f"Cannot read archive member {name!r}")
            with source_handle, target.open("wb") as output:
                shutil.copyfileobj(source_handle, output, length=8 * 1024 * 1024)
            extracted.append(target)
    return extracted


def commit_staged_snapshot(
    data_root: Path,
    source: Source,
    release: str,
    files: dict[str, Path],
) -> list[dict[str, Any]]:
    digests = {name: sha256(path) for name, path in files.items()}
    combined = hashlib.sha256(
        "\n".join(f"{name}:{digest}" for name, digest in sorted(digests.items())).encode()
    ).hexdigest()
    snapshot = acquisition_root(data_root) / "snapshots" / source.id / f"{release}--{combined[:12]}"
    snapshot.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for name, staged in files.items():
        destination = snapshot / name
        if destination.exists() and sha256(destination) != digests[name]:
            raise ValueError(f"Immutable snapshot collision: {destination}")
        if not destination.exists():
            os.replace(staged, destination)
        records.append(
            {
                "logical_name": name,
                "path": stored_path(data_root, destination),
                "bytes": destination.stat().st_size,
                "sha256": digests[name],
            }
        )
    return records


def update_public(args: argparse.Namespace, catalogue: Catalogue) -> int:
    manifest = load_manifest(args.root)
    selected_ids = args.source or [
        source.id
        for source in catalogue.sources.values()
        if source.get("access") not in {"manual", "browser-assisted"}
    ]
    unknown = sorted(set(selected_ids) - catalogue.sources.keys())
    if unknown:
        raise SystemExit(f"Unknown source(s): {', '.join(unknown)}")
    selected = [catalogue.sources[source_id] for source_id in selected_ids]
    manual = [
        source.id
        for source in selected
        if source.get("access") in {"manual", "browser-assisted"}
    ]
    if manual:
        raise SystemExit(f"Manual source(s) must be registered: {', '.join(manual)}")

    status: list[dict[str, Any]] = []
    updates: dict[str, Any] = {}
    for source in selected:
        emit("message", message=f"Check source: {source.id}")
        previous = manifest.get("sources", {}).get(source.id)
        if not is_due(source, previous, args.force):
            status.append({"source": source.id, "status": "current"})
            continue
        if args.dry_run:
            status.append({"source": source.id, "status": "due"})
            continue

        staging = acquisition_root(args.root) / ".staging" / source.id
        staging.mkdir(parents=True, exist_ok=True)
        inventory = source_inventory(source)
        inventory_downloads = [
            item
            for item in (inventory or {}).get("files", [])
            if item.get("download_url")
        ]
        if source.get("access") == "public-download" and inventory_downloads:
            files: dict[str, Path] = {}
            remote_files: dict[str, dict[str, Any]] = {}
            for item in inventory_downloads:
                logical_name = item["logical_name"]
                output = staging / logical_name
                remote = download(source, None, output, url=item["download_url"])
                if remote is None:
                    raise ValueError(
                        f"{source.id}: unexpected not-modified response for {logical_name}"
                    )
                files[logical_name] = output
                remote_files[logical_name] = remote
            errors = validate_input_files(source, files)
            if errors:
                raise ValueError(f"{source.id} validation failed: {'; '.join(errors)}")
            release = inventory_release(inventory)
            if release is None:
                raise ValueError(f"{source.id} public inventory has no release")
            records = commit_staged_snapshot(args.root, source, release, files)
            updates[source.id] = source_record(
                source,
                release,
                records,
                acquisition=source.get("access"),
                remote={"files": remote_files},
            )
            status.append(
                {"source": source.id, "status": "updated", "files": len(records)}
            )
            shutil.rmtree(staging)
            continue
        output_name = source.get("output_file")
        output = staging / output_name
        remote: dict[str, Any] | None = None
        if source.get("access") == "public-download":
            remote = download(source, previous, output)
            if remote is None:
                status.append({"source": source.id, "status": "not-modified"})
                continue
        elif source.get("access") in {"public-command", "authenticated-command"}:
            if source.get("access") == "authenticated-command":
                token_env = source.get("token_env")
                if not os.environ.get(token_env, ""):
                    raise ValueError(
                        f"{source.id} requires {token_env}; keep the token in the ignored .env file"
                    )
            previous_files = {
                item["logical_name"]: resolve_stored_path(args.root, item["path"])
                for item in (previous or {}).get("files", [])
            }
            replacements = {
                "{output}": str(output),
                "{previous}": str(previous_files.get(output_name, "")),
                "{previous_metadata}": str(
                    previous_files.get(source.get("metadata_file"), "")
                ),
            }
            command = []
            for part in source.get("command", []):
                for placeholder, replacement in replacements.items():
                    part = part.replace(placeholder, replacement)
                command.append(part)
            subprocess.run(command, cwd=REPOSITORY_ROOT, check=True)
        else:
            raise ValueError(f"Unsupported access mode for {source.id}: {source.get('access')}")

        files = {output_name: output}
        metadata_name = source.get("metadata_file")
        if metadata_name:
            files[metadata_name] = staging / metadata_name
        extract_members = list(source.get("extract_members", []))
        if extract_members:
            for path in safe_extract_tar(output, extract_members, staging):
                files[path.name] = path
        errors = validate_input_files(source, files)
        if errors:
            raise ValueError(f"{source.id} validation failed: {'; '.join(errors)}")
        digest = sha256(output)
        release = inventory_release(inventory) or (remote or {}).get("last_modified") or utc_now().date().isoformat()
        if metadata_name:
            metadata = json.loads((staging / metadata_name).read_text(encoding="utf-8"))
            release = metadata.get("red_list_version", release)
        release = str(release).replace("/", "-").replace(":", "-").replace(" ", "_")
        records = commit_staged_snapshot(args.root, source, release, files)
        updates[source.id] = source_record(
            source, release, records, acquisition=source.get("access"), remote=remote
        )
        status.append({"source": source.id, "status": "updated", "sha256": digest})
        shutil.rmtree(staging)

    if updates:
        activate(args.root, manifest, updates)
    print(json.dumps({"status": status, "manifest_updated": bool(updates)}, indent=2))
    return 0


def doctor(args: argparse.Namespace, catalogue: Catalogue) -> int:
    if args.profile not in catalogue.profiles:
        raise SystemExit(f"Unknown profile: {args.profile}")
    manifest = load_manifest(args.root)
    required = catalogue.profiles[args.profile].get("required_sources", [])
    results: list[dict[str, Any]] = []
    failures = 0
    for source_id in required:
        source = catalogue.sources[source_id]
        record = manifest.get("sources", {}).get(source_id)
        errors: list[str] = []
        if record is None:
            errors.append("not registered")
        else:
            files: dict[str, Path] = {}
            for item in record.get("files", []):
                path = resolve_stored_path(args.root, item["path"])
                files[item["logical_name"]] = path
                if not path.is_file():
                    errors.append(f"missing: {item['path']}")
                    continue
                if path.stat().st_size != item.get("bytes"):
                    errors.append(f"size changed: {item['path']}")
                if args.deep and sha256(path) != item.get("sha256"):
                    errors.append(f"checksum changed: {item['path']}")
            errors.extend(validate_input_files(source, files))
        status = "failed" if errors else "ready"
        failures += bool(errors)
        results.append({"source": source_id, "status": status, "errors": errors})
    report = {
        "schema_version": 1,
        "checked_at": iso_now(),
        "profile": args.profile,
        "status": "failed" if failures else "ready",
        "sources": results,
    }
    if args.output:
        atomic_json(args.output, report)
    print(json.dumps(report, indent=2))
    return 1 if failures else 0


def plan(args: argparse.Namespace, catalogue: Catalogue) -> int:
    manifest = load_manifest(args.root)
    rows = []
    for source in catalogue.sources.values():
        record = manifest.get("sources", {}).get(source.id)
        inventory = source_inventory(source)
        available_release = inventory_release(inventory)
        if source.get("access") in {"manual", "browser-assisted"}:
            due = record is None or (
                available_release is not None
                and available_release != (record or {}).get("release")
            )
        else:
            due = is_due(source, record, False)
        rows.append(
            {
                "source": source.id,
                "access": source.get("access"),
                "update_policy": source.get("update_policy"),
                "available_release": available_release,
                "registered_release": (record or {}).get("release"),
                "due": due,
                "expected_files": len((inventory or {}).get("files", [])) or None,
                "official_url": source.get("official_url"),
            }
        )
    print(json.dumps({"data_root": str(args.root.resolve()), "sources": rows}, indent=2))
    return 0


def targets(args: argparse.Namespace, catalogue: Catalogue) -> int:
    source = catalogue.sources.get(args.source)
    if source is None:
        raise SystemExit(f"Unknown source: {args.source}")
    inventory = source_inventory(source)
    if inventory is None:
        raise SystemExit(f"{source.id} has no configured download inventory")
    base_url = "https://www.iucnredlist.org/resources/files"
    requested_formats = set(getattr(args, "format", []) or [])
    files = [
        {
            **item,
            "url": item.get("download_url")
            or f"{base_url}/{item['file_id']}",
        }
        for item in inventory["files"]
        if not requested_formats or item.get("format") in requested_formats
    ]
    print(
        json.dumps(
            {
                "source": source.id,
                "release": inventory_release(inventory),
                "data_last_updated": inventory.get("data_last_updated"),
                "formats": sorted(requested_formats),
                "files": files,
            },
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan", help="show source access and update status")
    plan_parser.add_argument("--root", type=Path, required=True)

    targets_parser = subparsers.add_parser(
        "targets", help="show the versioned file inventory for a provider source"
    )
    targets_parser.add_argument("--source", default="iucn-spatial")
    targets_parser.add_argument(
        "--format",
        action="append",
        choices=("polygon", "point", "hydrobasin"),
        help="limit a mixed spatial inventory to one or more formats",
    )

    doctor_parser = subparsers.add_parser("doctor", help="validate the active source manifest")
    doctor_parser.add_argument("--root", type=Path, required=True)
    doctor_parser.add_argument("--profile", default="authorized")
    doctor_parser.add_argument("--deep", action="store_true", help="recompute every checksum")
    doctor_parser.add_argument("--output", type=Path)

    register_parser = subparsers.add_parser(
        "register", help="validate and register an authorized provider download"
    )
    register_parser.add_argument("--root", type=Path, required=True)
    register_parser.add_argument("--source", required=True)
    register_parser.add_argument("--release", required=True)
    register_inputs = register_parser.add_mutually_exclusive_group(required=True)
    register_inputs.add_argument(
        "--file", action="extend", nargs="+", metavar="[NAME=]PATH"
    )
    register_inputs.add_argument(
        "--inventory-dir",
        type=Path,
        help="map every configured provider filename from this directory",
    )
    register_parser.add_argument("--authorized", action="store_true")
    register_parser.add_argument(
        "--reference", action="store_true", help="reference files in place instead of copying them"
    )

    update_parser = subparsers.add_parser("update", help="refresh due public sources")
    update_parser.add_argument("--root", type=Path, required=True)
    update_parser.add_argument("--source", action="append")
    update_parser.add_argument("--force", action="store_true")
    update_parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    catalogue = load_catalogue(args.config)
    if args.command == "plan":
        return plan(args, catalogue)
    if args.command == "targets":
        return targets(args, catalogue)
    if args.command == "doctor":
        return doctor(args, catalogue)
    if args.command == "register":
        return register_manual(args, catalogue)
    if args.command == "update":
        return update_public(args, catalogue)
    parser.error(f"Unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted; the active manifest was not changed.", file=sys.stderr)
        raise SystemExit(130) from None
