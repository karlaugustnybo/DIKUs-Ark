#!/usr/bin/env python3
"""Build compact browser/runtime political boundary catalogues.

The source is a Natural Earth Admin-0 or Admin-1 archive. The output keeps only
the identifiers, labels, hierarchy metadata, and simplified geometry needed by
the map. Natural Earth data is in the public domain.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import zipfile
from pathlib import Path

import geopandas

FIELD_SETS = {
    "admin0": {
        "code": "ADM0_A3", "name": "ADMIN", "continent": "CONTINENT",
    },
    "admin1": {
        "code": "adm1_code", "name": "name", "parent_code": "adm0_a3",
        "region": "region", "boundary_type": "type_en",
    },
}


def build(
    source: Path, target: Path, tolerance: float = 0.05,
    level: str = "admin0",
) -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        with zipfile.ZipFile(source) as archive:
            archive.extractall(temporary_directory)
        shapefiles = list(Path(temporary_directory).glob("*.shp"))
        if len(shapefiles) != 1:
            raise ValueError("Expected exactly one shapefile in the source archive")
        frame = geopandas.read_file(shapefiles[0]).to_crs(4326)

    fields = FIELD_SETS[level]
    frame = frame[[*fields.values(), "geometry"]].rename(
        columns={source: target for target, source in fields.items()}
    )
    if frame["code"].duplicated().any() or frame["code"].isna().any():
        raise ValueError("Jurisdiction codes must be present and unique")
    missing_names = frame["name"].isna() | frame["name"].astype(str).str.strip().eq("")
    if missing_names.any():
        if "parent_code" in frame:
            frame.loc[missing_names, "name"] = (
                "Unspecified " + frame.loc[missing_names, "parent_code"].fillna("").astype(str) + " area"
            )
        else:
            frame.loc[missing_names, "name"] = frame.loc[missing_names, "code"].astype(str)
    frame["geometry"] = frame.geometry.simplify(tolerance, preserve_topology=True)
    frame = frame.sort_values(["name", "code"]).reset_index(drop=True)
    catalogue = json.loads(frame.to_json(drop_id=True))
    label = "Admin-0 countries" if level == "admin0" else "Admin-1 states and provinces"
    source_slug = "10m-admin-0-countries" if level == "admin0" else "10m-admin-1-states-provinces"
    catalogue.update({
        "name": f"Natural Earth {label}",
        "framework": level,
        "source": f"https://www.naturalearthdata.com/downloads/10m-cultural-vectors/{source_slug}/",
        "source_version": "5.1.1",
        "license": "Public domain",
        "assignment": "H3 cell polygon intersection",
    })
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(catalogue, separators=(",", ":")) + "\n")
    print(f"Exported {len(frame)} {level} boundaries to {target}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tolerance", type=float, default=0.05)
    parser.add_argument("--level", choices=tuple(FIELD_SETS), default="admin0")
    arguments = parser.parse_args()
    build(arguments.source, arguments.output, arguments.tolerance, arguments.level)


if __name__ == "__main__":
    main()
