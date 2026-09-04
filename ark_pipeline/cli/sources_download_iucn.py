#!/usr/bin/env python3
"""Download the current IUCN Red List assessment catalogue through API v4.

This intentionally downloads only the paginated assessment catalogue. It does
not issue one detail request per assessment and does not attempt to replace the
provider's bulk spatial downloads.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://api.iucnredlist.org/api/v4"
TOKEN_ENV = "IUCN_REDLIST_KEY"
USER_AGENT = "Ark-IV-IUCN-catalog/1.0"


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.part")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class IucnApi:
    def __init__(self, token: str, base_url: str, delay: float) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.delay = delay
        self.last_request_at = 0.0

    def get(
        self, endpoint: str, query: dict[str, str | int] | None = None, attempts: int = 8
    ) -> tuple[Any, dict[str, str]]:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            wait = self.delay - (time.monotonic() - self.last_request_at)
            if wait > 0:
                time.sleep(wait)
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self.token}",
                    "User-Agent": USER_AGENT,
                },
            )
            try:
                self.last_request_at = time.monotonic()
                with urllib.request.urlopen(request, timeout=120) as response:
                    body = json.loads(response.read().decode("utf-8"))
                    headers = {key.lower(): value for key, value in response.headers.items()}
                    return body, headers
            except urllib.error.HTTPError as error:
                last_error = error
                if error.code not in {429, 500, 502, 503, 504} or attempt == attempts:
                    raise
                retry_after = error.headers.get("Retry-After")
                backoff = float(retry_after) if retry_after else min(60, 2**attempt)
            except (TimeoutError, urllib.error.URLError, json.JSONDecodeError) as error:
                last_error = error
                if attempt == attempts:
                    raise
                backoff = min(60, 2**attempt)
            time.sleep(backoff + random.random())
        if last_error:
            raise last_error
        raise RuntimeError("IUCN request failed without an error")


def only_list(value: Any, preferred: tuple[str, ...] = ()) -> list[Any]:
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON array or object, received {type(value).__name__}")
    for key in preferred:
        if isinstance(value.get(key), list):
            return value[key]
    candidates = [item for item in value.values() if isinstance(item, list)]
    if len(candidates) != 1:
        raise ValueError(f"Cannot identify result array in response keys {sorted(value)}")
    return candidates[0]


def kingdom_name(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("name", "kingdom_name", "scientific_name"):
            if value.get(key):
                return str(value[key])
    raise ValueError(f"Cannot identify kingdom name in {value!r}")


def assessment_identity(value: Any) -> str:
    if not isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    for key in ("assessment_id", "assessmentId", "id"):
        if value.get(key) is not None:
            return str(value[key])
    return json.dumps(value, sort_keys=True)


def safe_component(value: str) -> str:
    safe = "".join(character if character.isalnum() else "_" for character in value)
    return safe.strip("_") or "unknown"


def reuse_if_current(
    api: IucnApi,
    output: Path,
    metadata_path: Path,
    previous: Path | None,
    previous_metadata: Path | None,
) -> tuple[bool, str]:
    version_body, _ = api.get("information/red_list_version")
    release = str(version_body.get("red_list_version"))
    if not release or release == "None":
        raise ValueError(f"Unexpected Red List version response: {version_body!r}")
    if not previous or not previous.is_file() or not previous_metadata or not previous_metadata.is_file():
        return False, release
    metadata = json.loads(previous_metadata.read_text(encoding="utf-8"))
    if metadata.get("red_list_version") != release or metadata.get("status") != "complete":
        return False, release
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(previous, output)
        os.link(previous_metadata, metadata_path)
    except OSError:
        shutil.copy2(previous, output)
        shutil.copy2(previous_metadata, metadata_path)
    return True, release


def download_catalogue(
    api: IucnApi,
    output: Path,
    metadata_path: Path,
    previous: Path | None,
    previous_metadata: Path | None,
) -> dict[str, Any]:
    reused, release = reuse_if_current(api, output, metadata_path, previous, previous_metadata)
    if reused:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        return {**metadata, "reused": True}

    work = output.parent / f".{output.name}.pages" / safe_component(release)
    work.mkdir(parents=True, exist_ok=True)
    kingdom_body, _ = api.get("taxa/kingdom")
    kingdoms = sorted(
        {kingdom_name(value) for value in only_list(kingdom_body, ("kingdoms", "kingdom_names"))}
    )
    if not kingdoms:
        raise ValueError("IUCN returned no kingdoms")

    rows_by_kingdom: dict[str, list[dict[str, Any]]] = {}
    page_counts: dict[str, int] = {}
    for kingdom in kingdoms:
        directory = work / safe_component(kingdom)
        directory.mkdir(parents=True, exist_ok=True)
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        total: int | None = None
        for page in range(1, 10_001):
            page_path = directory / f"{page:05d}.json"
            header_path = directory / f"{page:05d}.headers.json"
            if page_path.is_file() and header_path.is_file():
                body = json.loads(page_path.read_text(encoding="utf-8"))
                headers = json.loads(header_path.read_text(encoding="utf-8"))
            else:
                endpoint = f"taxa/kingdom/{urllib.parse.quote(kingdom, safe='')}"
                body, headers = api.get(
                    endpoint,
                    {"latest": "true", "scope_code": "1", "page": page},
                )
                atomic_json(page_path, body)
                atomic_json(header_path, headers)
            page_rows = only_list(body, ("assessments", "result"))
            if headers.get("total-count"):
                total = int(headers["total-count"])
            if not page_rows:
                page_counts[kingdom] = page
                break
            new_rows = []
            for row in page_rows:
                identity = assessment_identity(row)
                if identity not in seen:
                    seen.add(identity)
                    new_rows.append(row)
            if not new_rows:
                raise ValueError(f"IUCN pagination repeated a page for {kingdom} at page {page}")
            rows.extend(new_rows)
            print(
                f"IUCN {release}: {kingdom} page {page}, {len(rows)}"
                + (f"/{total}" if total is not None else ""),
                file=sys.stderr,
                flush=True,
            )
            if total is not None and len(rows) >= total:
                page_counts[kingdom] = page
                break
        else:
            raise ValueError(f"IUCN pagination exceeded 10,000 pages for {kingdom}")
        rows_by_kingdom[kingdom] = rows

    temporary = output.with_name(f".{output.name}.part")
    with temporary.open("w", encoding="utf-8") as handle:
        for kingdom in kingdoms:
            for row in rows_by_kingdom[kingdom]:
                handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
    os.replace(temporary, output)
    metadata = {
        "schema_version": 1,
        "status": "complete",
        "generated_at": iso_now(),
        "red_list_version": release,
        "api_base_url": api.base_url,
        "scope_code": "1",
        "latest": True,
        "kingdoms": kingdoms,
        "rows": sum(len(rows) for rows in rows_by_kingdom.values()),
        "rows_by_kingdom": {key: len(value) for key, value in rows_by_kingdom.items()},
        "pages_by_kingdom": page_counts,
        "reused": False,
    }
    atomic_json(metadata_path, metadata)
    return metadata


def optional_path(value: str | None) -> Path | None:
    return Path(value).expanduser() if value else None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--previous", default="")
    parser.add_argument("--previous-metadata", default="")
    parser.add_argument("--base-url", default=os.environ.get("IUCN_API_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--delay", type=float, default=2.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = os.environ.get(TOKEN_ENV)
    if not token:
        raise SystemExit(
            f"{TOKEN_ENV} is required. Create a personal v4 token at "
            "https://api.iucnredlist.org/users/sign_up and keep it in .env."
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    metadata_path = args.output.with_name("iucn_assessment_catalog.metadata.json")
    api = IucnApi(token, args.base_url, args.delay)
    metadata = download_catalogue(
        api,
        args.output,
        metadata_path,
        optional_path(args.previous),
        optional_path(args.previous_metadata),
    )
    print(json.dumps(metadata, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
