from __future__ import annotations

import asyncio
import math
import re
from pathlib import Path

from litestar import Litestar, Request, Response, get
from litestar.config.cors import CORSConfig
from litestar.datastructures import State
from litestar.exceptions import HTTPException, NotFoundException
from litestar.openapi.config import OpenAPIConfig
from litestar.params import Parameter
from litestar.response import File

from backend.config import get_settings
from backend.db import database_lifespan, get_pool
from backend.models import (
    CellDetailsResponse,
    CellSpeciesRow,
    CellStats,
    ExportInfo,
    HealthResponse,
    SpeciesPage,
    SpeciesRow,
    StatsResponse,
)


settings = get_settings()
VALID_SYSTEMS = {"Terrestrial", "Freshwater", "Marine"}
VALID_SORTS = {
    "species_name": "species_name",
    "family": "family",
    "redlist_category": "redlist_category",
    "threat_score": "threat_score",
    "dna_level": "dna_level_score",
    "priority": "priority",
}


def _h3_to_bigint(value: str) -> int:
    try:
        return int(value, 16)
    except ValueError as exc:
        raise NotFoundException(detail="Invalid H3 index") from exc


@get("/api/health", operation_id="getHealth")
async def health(state: State) -> HealthResponse:
    pool = get_pool(state)
    database = "ok"
    try:
        await pool.fetchval("SELECT 1")
    except Exception:
        database = "unavailable"
    tiles = "ok" if settings.pmtiles_path.is_file() else "missing"
    return HealthResponse(status="ok" if database == tiles == "ok" else "degraded", database=database, tiles=tiles)


@get("/api/stats", operation_id="getStats")
async def stats(state: State) -> StatsResponse:
    pool = get_pool(state)
    row = await pool.fetchrow(
        """
        SELECT total, critically_endangered, edge_species, needs_dna_sampling,
               res3_cells, res7_cells
        FROM app_stats WHERE singleton = TRUE
        """
    )
    return StatsResponse(**dict(row))


@get("/api/species", operation_id="getSpecies")
async def species_page(
    state: State,
    search: str = "",
    sort: str = "priority",
    order: str = "desc",
    page: int = Parameter(default=1, ge=1),
    per_page: int = Parameter(default=10, ge=1, le=100),
    cr: float = 4.0,
    en: float = 3.0,
    vu: float = 2.0,
    nt: float = 1.0,
    dd: float = 2.0,
    lc: float = 0.1,
    sp: float = 2.0,
    gen: float = 3.0,
    fam: float = 4.0,
    samp: float = 0.0,
    cov: float = 0.0,
) -> SpeciesPage:
    pool = get_pool(state)
    sort_sql = VALID_SORTS.get(sort, "priority")
    order_sql = "ASC" if order.lower() == "asc" else "DESC"
    pattern = search.strip() or ".*"
    try:
        re.compile(pattern)
    except re.error as exc:
        raise HTTPException(status_code=400, detail=f"Invalid regular expression: {exc}") from exc
    total = await pool.fetchval(
        "SELECT COUNT(*) FROM species WHERE species_name ~* $1 OR COALESCE(family, '') ~* $1",
        pattern,
    )
    total_pages = max(1, math.ceil(total / per_page))
    page = min(page, total_pages)
    values = [cr, en, vu, nt, dd, lc, fam, gen, sp, samp, pattern, per_page, (page - 1) * per_page]
    rows = await pool.fetch(
        f"""
        WITH scored AS (
            SELECT COALESCE(species_name, '') AS species_name, COALESCE(family, '') AS family,
                COALESCE(redlist_category, 'Not Assessed') AS redlist_category,
                CASE redlist_category
                    WHEN 'Critically Endangered' THEN $1::double precision WHEN 'Endangered' THEN $2::double precision
                    WHEN 'Vulnerable' THEN $3::double precision WHEN 'Near Threatened' THEN $4::double precision
                    WHEN 'Data Deficient' THEN $5::double precision WHEN 'Least Concern' THEN $6::double precision ELSE 0
                END::float AS threat_score,
                CASE WHEN family_has_dna = false THEN 'Missing Family (' || $7::double precision::text || ')'
                    WHEN genus_has_dna = false THEN 'Missing Genus (' || $8::double precision::text || ')'
                    WHEN has_dna_species_level = false THEN 'Missing Species (' || $9::double precision::text || ')'
                    ELSE 'Already Sampled' END AS dna_level,
                CASE WHEN family_has_dna = false THEN $7::double precision WHEN genus_has_dna = false THEN $8::double precision
                    WHEN has_dna_species_level = false THEN $9::double precision ELSE $10::double precision END::float AS dna_level_score
            FROM species
            WHERE species_name ~* $11::text OR COALESCE(family, '') ~* $11::text
        )
        SELECT species_name, family, redlist_category, threat_score, dna_level,
               (threat_score * dna_level_score)::float AS priority
        FROM scored
        ORDER BY {sort_sql} {order_sql}, species_name ASC
        LIMIT $12::int OFFSET $13::int
        """,
        *values,
    )
    return SpeciesPage(
        rows=[SpeciesRow(**dict(row)) for row in rows],
        page=page,
        total_pages=total_pages,
        total=total,
    )


@get("/api/cells/{h3_index:str}/species", operation_id="getCellSpecies")
async def cell_species(
    state: State,
    h3_index: str,
    resolution: int = 3,
    system: str = "",
) -> CellDetailsResponse:
    if resolution not in (3, 7):
        raise NotFoundException(detail="Resolution must be 3 or 7")
    pool = get_pool(state)
    h3_value = _h3_to_bigint(h3_index)
    system_value = system if system in VALID_SYSTEMS else None
    rows = await pool.fetch(
        """
        SELECT s.species_name, COALESCE(s.family, '') AS family,
               COALESCE(s.redlist_category, 'Not Assessed') AS redlist_category,
               s.has_dna_species_level, s.genus_has_dna, s.family_has_dna
        FROM cell_species cs
        JOIN species s USING (gbif_accepted_id)
        WHERE cs.h3_index = $1 AND cs.resolution = $2
          AND ($3::text IS NULL OR EXISTS (
              SELECT 1 FROM species_systems ss
              WHERE ss.gbif_accepted_id = s.gbif_accepted_id AND ss.system = $3
          ))
        ORDER BY CASE s.redlist_category
            WHEN 'Critically Endangered' THEN 1 WHEN 'Endangered' THEN 2
            WHEN 'Vulnerable' THEN 3 WHEN 'Near Threatened' THEN 4
            WHEN 'Data Deficient' THEN 5 WHEN 'Least Concern' THEN 6 ELSE 7 END,
            s.species_name
        """,
        h3_value,
        resolution,
        system_value,
    )
    stats = CellStats()
    result: list[CellSpeciesRow] = []
    categories = {
        "Critically Endangered": "CR", "Endangered": "EN", "Vulnerable": "VU",
        "Near Threatened": "NT", "Data Deficient": "DD", "Least Concern": "LC",
    }
    for row in rows:
        stats.total += 1
        if category := categories.get(row["redlist_category"]):
            setattr(stats, category, getattr(stats, category) + 1)
        if not row["has_dna_species_level"]:
            stats.missing_species_dna += 1
        if not row["genus_has_dna"]:
            stats.missing_genus_dna += 1
        if not row["family_has_dna"]:
            stats.missing_family_dna += 1
            dna_level = "Missing Family"
        elif not row["genus_has_dna"]:
            dna_level = "Missing Genus"
        elif not row["has_dna_species_level"]:
            dna_level = "Missing Species"
        else:
            dna_level = "Sampled"
        result.append(CellSpeciesRow(row["species_name"], row["family"], row["redlist_category"], dna_level))
    return CellDetailsResponse(h3_index=h3_index, species=result, stats=stats)


@get("/api/exports", operation_id="getExports")
async def exports() -> list[ExportInfo]:
    return [
        ExportInfo("parquet", "/exports/species.parquet", "application/vnd.apache.parquet"),
        ExportInfo("parquet", "/exports/cell_species.parquet", "application/vnd.apache.parquet"),
    ]


def _static_file(path: Path, media_type: str) -> File:
    if not path.is_file():
        raise NotFoundException(detail=f"Static build artifact not found: {path.name}")
    return File(path=path, media_type=media_type, content_disposition_type="inline")


@get("/tiles/{filename:str}", include_in_schema=False)
async def tiles(request: Request, filename: str) -> File | Response[bytes]:
    if filename == settings.map_metadata_path.name:
        if not settings.map_metadata_path.is_file():
            raise NotFoundException(detail="Map metadata build artifact not found")
        return File(
            path=settings.map_metadata_path,
            media_type="application/json",
            content_disposition_type="inline",
            headers={"Cache-Control": "no-cache"},
        )
    if filename != settings.pmtiles_path.name:
        raise NotFoundException()
    path = settings.pmtiles_path
    if not path.is_file():
        raise NotFoundException(detail="PMTiles build artifact not found")
    size = path.stat().st_size
    range_header = request.headers.get("range")
    if not range_header:
        return File(
            path=path,
            media_type="application/vnd.pmtiles",
            content_disposition_type="inline",
            headers={"Accept-Ranges": "bytes"},
        )
    match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header.strip())
    if not match:
        return Response(content=b"", status_code=416, headers={"Content-Range": f"bytes */{size}"})
    start = int(match.group(1))
    end = min(int(match.group(2)) if match.group(2) else size - 1, size - 1)
    if start > end or start >= size:
        return Response(content=b"", status_code=416, headers={"Content-Range": f"bytes */{size}"})

    def read_range() -> bytes:
        with path.open("rb") as stream:
            stream.seek(start)
            return stream.read(end - start + 1)

    content = await asyncio.to_thread(read_range)
    return Response(
        content=content,
        status_code=206,
        media_type="application/vnd.pmtiles",
        headers={
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(len(content)),
            "Cache-Control": "public, max-age=3600",
        },
    )


@get("/exports/{filename:str}", include_in_schema=False)
async def export_file(filename: str) -> File:
    if Path(filename).name != filename:
        raise NotFoundException()
    return _static_file(settings.export_dir / filename, "application/vnd.apache.parquet")


app = Litestar(
    route_handlers=[health, stats, species_page, cell_species, exports, tiles, export_file],
    lifespan=[database_lifespan],
    cors_config=CORSConfig(allow_origins=[settings.frontend_origin]),
    openapi_config=OpenAPIConfig(
        title="Ark-IV API",
        version="1.0.0",
        description="Typed serving API for species and H3 cell details. Map movement is served by PMTiles.",
        path="/schema",
    ),
)
