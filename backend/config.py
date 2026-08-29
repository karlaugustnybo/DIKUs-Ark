from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables or ``.env``."""

    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    database_url: str = "postgresql://ark:ark@127.0.0.1:5432/ark_iv"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    api_workers: int = 1
    db_pool_min_size: int = 1
    db_pool_max_size: int = 5
    load_expanded_cell_species: bool = False
    frontend_origin: str = "http://127.0.0.1:5173"
    data_dir: Path = ROOT / "data"
    source_duckdb_path: Path = ROOT / "data" / "Ark-IV.duckdb"
    build_duckdb_path: Path = ROOT / "data" / "precomputed_cache.duckdb"
    export_dir: Path = ROOT / "data" / "exports"
    tile_dir: Path = ROOT / "data" / "tiles"
    pmtiles_path: Path = ROOT / "data" / "tiles" / "priorities.pmtiles"
    map_metadata_path: Path = ROOT / "data" / "tiles" / "map-metadata.json"
    # Country selection includes both land and the matching maritime zone.
    jurisdictions_path: Path = ROOT / "data" / "boundaries" / "country-scope.geojson"
    admin1_boundaries_path: Path = ROOT / "app" / "static" / "data" / "boundaries" / "admin1.geojson"
    municipality_boundaries_path: Path = ROOT / "app" / "static" / "data" / "boundaries" / "municipality.geojson"
    eez_boundaries_path: Path = ROOT / "data" / "boundaries" / "eez.geojson"
    conservation_boundaries_path: Path = ROOT / "app" / "static" / "data" / "boundaries" / "conservation-framework.geojson"
    boundary_frameworks_path: Path = ROOT / "app" / "static" / "data" / "boundary-frameworks.json"
    validation_report_path: Path = ROOT / "data" / "validation" / "build-validation.json"
    tippecanoe_bin: str = "tippecanoe"
    h3_res3_parquet: Path = ROOT / "data" / "h3_res3_species.parquet"
    h3_res7_parquet: Path = ROOT / "data" / "h3_res7_species.parquet"
    h3_id_crosswalk_path: Path | None = None
    h3_input_dir: Path = ROOT / "data" / "h3_pairs"
    h3_encoded_dir: Path = ROOT / "data" / "h3_encoded"
    h3_aggregated_dir: Path = ROOT / "data" / "h3_aggregated"
    res7_parts_dir: Path | None = None
    res7_aggregate_parts_dir: Path | None = None
    duckdb_scratch_dir: Path = ROOT / "data" / "duckdb_scratch"
    duckdb_memory_limit: str = "4GB"
    duckdb_threads: int = 1

    @field_validator(
        "data_dir",
        "source_duckdb_path",
        "build_duckdb_path",
        "export_dir",
        "tile_dir",
        "pmtiles_path",
        "map_metadata_path",
        "jurisdictions_path",
        "admin1_boundaries_path",
        "municipality_boundaries_path",
        "eez_boundaries_path",
        "conservation_boundaries_path",
        "boundary_frameworks_path",
        "validation_report_path",
        "h3_res3_parquet",
        "h3_res7_parquet",
        "h3_input_dir",
        "h3_encoded_dir",
        "h3_aggregated_dir",
        "duckdb_scratch_dir",
        mode="after",
    )
    @classmethod
    def resolve_path(cls, value: Path) -> Path:
        return value if value.is_absolute() else (ROOT / value).resolve()

    @field_validator(
        "h3_id_crosswalk_path",
        "res7_parts_dir",
        "res7_aggregate_parts_dir",
        mode="after",
    )
    @classmethod
    def resolve_optional_path(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        return value if value.is_absolute() else (ROOT / value).resolve()

    @field_validator(
        "h3_id_crosswalk_path",
        "res7_parts_dir",
        "res7_aggregate_parts_dir",
        mode="before",
    )
    @classmethod
    def empty_path_is_none(cls, value: object) -> object:
        return None if value == "" else value


@lru_cache
def get_settings() -> Settings:
    return Settings()
