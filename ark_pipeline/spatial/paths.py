"""Shared selection of installed global boundaries and the bundled preview."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADM2_ROOT = ROOT / "data/boundaries/adm2"


def municipality_geometry_path() -> Path:
    current = ADM2_ROOT / "current/municipality.geojson"
    return current if current.is_file() else ROOT / "app/static/data/boundaries/municipality.geojson"
