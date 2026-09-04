"""Fail when the prospective Git commit contains private or unsafe artifacts."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_GITHUB_FILE_BYTES = 95 * 1024 * 1024
REQUIRED_FILES = (
    "DATA_POLICY.md",
    "LICENSE",
    "NOTICE.md",
    "ai_declaration.md",
    "docs/publication_checklist.md",
)
ALLOWED_DATA_FILES = {
    "data/exports/.gitkeep",
    "data/tiles/.gitkeep",
}
BLOCKED_EXACT_PATHS = {
    "data/Ark-IV.duckdb",
    "data/precomputed_cache.duckdb",
}
BLOCKED_SUFFIXES = {
    ".arrow",
    ".csv",
    ".db",
    ".duckdb",
    ".feather",
    ".ipc",
    ".mov",
    ".parquet",
    ".pmtiles",
    ".sqlite",
    ".sqlite3",
    ".tsv",
}
BLOCKED_DIRECTORIES = {
    "archive", "acquisition", "denmark_prototype", ".tmp", ".venv",
    "node_modules", "__pycache__", ".ruff_cache", ".pytest_cache", ".svelte-kit",
}
SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"gh[oprs]_[A-Za-z0-9]{36,}"),
    re.compile(rb"sk-[A-Za-z0-9_-]{32,}"),
)


def git_output(*args: str) -> bytes:
    return subprocess.check_output(("git", *args), cwd=ROOT)


def candidate_files() -> list[Path]:
    output = git_output("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return sorted(
        path
        for raw in output.split(b"\0")
        if raw and (path := ROOT / raw.decode("utf-8", errors="surrogateescape")).is_file()
    )


def worktree_errors(paths: list[Path]) -> list[str]:
    errors: list[str] = []
    relative_paths = {path.relative_to(ROOT).as_posix() for path in paths}

    for required in REQUIRED_FILES:
        if required not in relative_paths:
            errors.append(f"missing required release document: {required}")

    for path in paths:
        relative = path.relative_to(ROOT).as_posix()
        suffix = path.suffix.lower()

        if any(part in BLOCKED_DIRECTORIES for part in Path(relative).parts[:-1]):
            errors.append(f"local-only artifact must remain outside Git: {relative}")
        if (path.name == ".env" or path.name.startswith(".env.")) and path.name != ".env.example":
            errors.append(f"environment secrets must remain outside Git: {relative}")
        if path.is_symlink():
            errors.append(f"file symlinks require explicit release review: {relative}")
            continue
        if relative in BLOCKED_EXACT_PATHS:
            errors.append(f"restricted database must not be published: {relative}")
        if relative.startswith("data/") and relative not in ALLOWED_DATA_FILES:
            errors.append(f"generated or source data must remain outside Git: {relative}")
        if suffix in BLOCKED_SUFFIXES:
            errors.append(f"blocked data/binary format in prospective commit: {relative}")
        if path.stat().st_size >= MAX_GITHUB_FILE_BYTES:
            errors.append(f"file is too large for a normal GitHub release: {relative}")

        if path.stat().st_size <= 5 * 1024 * 1024:
            content = path.read_bytes()
            if any(pattern.search(content) for pattern in SECRET_PATTERNS):
                errors.append(f"possible credential or private key detected: {relative}")

    manifest_path = ROOT / "app/static/data/boundary-frameworks.json"
    if manifest_path in paths:
        try:
            frameworks = json.loads(manifest_path.read_text())["frameworks"]
            for framework in frameworks:
                if framework.get("status") == "ready":
                    for key in ("license", "source_url"):
                        if not framework.get(key):
                            errors.append(
                                f"ready boundary framework lacks {key}: {framework.get('id', '?')}"
                            )
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            errors.append(f"invalid boundary attribution manifest: {error}")

    return sorted(set(errors))


def history_errors() -> list[str]:
    errors: list[str] = []
    for path in sorted(BLOCKED_EXACT_PATHS):
        commits = git_output("log", "--all", "--format=%H", "--", path).splitlines()
        if commits:
            errors.append(
                f"restricted artifact remains in Git history ({len(commits)} commit(s)): {path}"
            )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--history",
        action="store_true",
        help="also fail if known restricted databases remain anywhere in local Git history",
    )
    args = parser.parse_args()

    paths = candidate_files()
    errors = worktree_errors(paths)
    if args.history:
        errors.extend(history_errors())

    if errors:
        print("Release check failed:")
        for error in errors:
            print(f"  - {error}")
        return 1

    scope = "prospective commit and history" if args.history else "prospective commit"
    total_mebibytes = sum(path.stat().st_size for path in paths) / (1024 * 1024)
    print(
        f"Release check passed for {scope} "
        f"({len(paths)} files, {total_mebibytes:.1f} MiB inspected)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
