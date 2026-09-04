"""Read census envelopes without reconstructing polygon topology or exporting WKB."""

from __future__ import annotations

import warnings
from pathlib import Path

import numpy as np
import pyogrio
import shapely


def read_census_bounds(path: Path, layer: str):
    """Use all ring coordinates, without classifying rings as shells or holes.

    This option is ONLY safe for envelopes, never for coverage or polygon area.
    Call in a dedicated scan process, not alongside other GDAL work in threads:
    GDAL configuration is process-global. Restore it even when the read fails.
    """
    previous = pyogrio.get_gdal_config_option("OGR_ORGANIZE_POLYGONS")
    try:
        pyogrio.set_gdal_config_options({"OGR_ORGANIZE_POLYGONS": "SKIP"})
        with warnings.catch_warnings():
            # SKIP intentionally leaves rings as separate polygons. Suppress only
            # that expected diagnostic; corruption and other warnings stay visible.
            warnings.filterwarnings(
                "ignore", category=RuntimeWarning,
                message=r"Geometry of polygon of fid \d+ cannot be translated to Simple Geometry\. All polygons will be contained in a multipolygon\.",
            )
            return pyogrio.read_bounds(path, layer=layer)
    finally:
        pyogrio.set_gdal_config_options({"OGR_ORGANIZE_POLYGONS": previous})


def iter_census_batches(path: Path, layer: str, batch_rows: int):
    """Join small native envelope arrays to streamed attributes by source FID.

    Memory is proportional to feature count, not the layer's vertex count.
    Null/empty/degenerate envelopes need the original WKB to preserve the exact
    null-geometry exclusion and empty-geometry failure semantics.
    """
    fids, bounds = read_census_bounds(path, layer)
    positions = {int(fid): index for index, fid in enumerate(fids)}
    if len(positions) != len(fids):
        raise ValueError(f"Duplicate census FIDs in {layer}")
    with pyogrio.open_arrow(
        path, layer=layer, columns=["id_no", "presence", "origin", "seasonal"],
        read_geometry=False, return_fids=True, batch_size=batch_rows, use_pyarrow=True,
    ) as (metadata, reader):
        fid_name = metadata["fid_column"]
        for batch in reader:
            rows = batch.to_pylist()
            envelopes, suspect = [], []
            for row in rows:
                fid = row[fid_name]
                if fid not in positions:
                    raise ValueError(f"Census attributes/bounds FID mismatch in {layer}: {fid}")
                envelope = bounds[:, positions.pop(fid)]
                envelopes.append(envelope)
                if (not np.isfinite(envelope).all()
                        or envelope[0] == envelope[2] or envelope[1] == envelope[3]):
                    suspect.append(fid)
            geometries = {}
            if suspect:
                geometry_metadata, table = pyogrio.read_arrow(
                    path, layer=layer, columns=[], fids=suspect, return_fids=True,
                )
                geometry_name = geometry_metadata["geometry_name"] or "wkb_geometry"
                geometries = {row[geometry_metadata["fid_column"]]: row[geometry_name]
                              for row in table.to_pylist()}
                if set(geometries) != set(suspect) or len(table) != len(suspect):
                    raise ValueError(f"Census geometry FID mismatch in {layer}")
            result = []
            for row, envelope in zip(rows, envelopes):
                # A nonempty marker preserves the common row-policy function
                # without materializing any ordinary geometry in Python.
                wkb = geometries.get(row[fid_name], b"bounds-only")
                result.append((row, wkb, envelope))
            yield result
    if positions:
        raise ValueError(f"Census bounds lack attributes in {layer}: {len(positions)} rows")


def census_bounds(wkb: bytes, envelope):
    """Decode only exceptional envelopes, after applying the row exclusions."""
    if wkb != b"bounds-only":
        return shapely.from_wkb(wkb).bounds
    return envelope
