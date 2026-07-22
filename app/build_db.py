# ---------------------------------------------------------------------------
# This file creates a beautiful decomposed database from a collection of 
# different ones. The resulting tables are:
# 
#   1. SpecInfo(gbif_accepted_id, <info>): Contains info of species based on their GBIF id.
#   2. H3Centroids(h3_index, longitude, latitude, is_land, is_sea): Connects H3 cells 
#      to their location and classifies them as land/sea based on Denmark's borders.
#   3. H3Res3Species(h3_index, gbif_ids): Connects cells to a list of the species living 
#      in them. Violates 1NF, but if not, the disk usage would be large, and 
#      performance would be worse.
#   4. H3Res3Systems(h3_index, system): Connects cells to the system(s) that we
#      qualify them as.
#   5. SpecSystems(gbif_accepted_id, system): Connects species to the system(s) that they
#      live in.
# 
#   All of the tables above prefixed with 'H3Res3' also get constructed for H3Res7 cells.
# ---------------------------------------------------------------------------

import duckdb
import h3
import sys
import os
import shutil
from pathlib import Path
import geopandas as gpd
from shapely.geometry import Polygon
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# 1. Paths & initialisation
# ---------------------------------------------------------------------------
# Every location is configurable in the repository-level .env file.
ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def configured_path(name, default):
    value = Path(os.environ.get(name, default)).expanduser()
    return value if value.is_absolute() else (ROOT / value).resolve()


DATA_DIR = configured_path('DATA_DIR', 'data')
    
# Two DuckDB files — tabular (species/IUCN/EDGE/GoaT/ToL) and H3 (map).
H3_DB_PATH      = configured_path('H3_DUCKDB_PATH', str(DATA_DIR / 'denmark_h3.duckdb'))
TABULAR_DB_PATH = configured_path('TABULAR_DUCKDB_PATH', str(DATA_DIR / 'denmark_tabular.duckdb'))
BORDERS_PATH    = configured_path('BORDERS_PATH', str(DATA_DIR / 'denmark_borders.parquet'))
DB_PATH         = configured_path('SOURCE_DUCKDB_PATH', str(DATA_DIR / 'Ark-IV.duckdb'))

# Check if DB_PATH is already in use
if Path(DB_PATH).exists():
    # Ask if wanting to rebuild
    while (True):
        rebuild_answer = input('\nArk-IV database is already build. Do you wish to reuild? [y/n] ')
        print()
        if rebuild_answer in ['y', 'n']:
            wished_rebuild = rebuild_answer == 'y'
            break
    # If not, abort
    if not wished_rebuild:
        print('Aborting as you do not wish to rebuild.')
        sys.exit()
    # If, delete original file for a new one to be created
    else:
        try:
            # Create (and potentially overwrite) backup
            DB_BACKUP_PATH = DATA_DIR / 'Ark-IV_backup.duckdb'
            if Path(DB_BACKUP_PATH).exists():
                os.remove(DB_BACKUP_PATH)
            shutil.copy(DB_PATH, DB_BACKUP_PATH)

            # Remove old database (new stored as backup) for a new one to be created
            os.remove(DB_PATH)
            print('Existing database backed up. Rebuilding...')
        except OSError: pass


# Raw H3 hexagon → species list mappings (used for dynamic map queries).
H3_RES3_PARQUET = configured_path('H3_RES3_PARQUET', str(DATA_DIR / 'h3_res3_species.parquet'))
H3_RES7_PARQUET = configured_path('H3_RES7_PARQUET', str(DATA_DIR / 'h3_res7_species.parquet'))

# Connection that ATTACHes both databases read-only
MAIN_CON = duckdb.connect(DB_PATH)
MAIN_CON.execute(f"ATTACH '{H3_DB_PATH}' AS h3 (READ_ONLY)")
MAIN_CON.execute(f"ATTACH '{TABULAR_DB_PATH}' AS tabular (READ_ONLY)")

# Create SpecInfo table
print('Building SpecInfo table...')

MAIN_CON.execute("""
    CREATE TABLE SpecInfo (
        gbif_accepted_id VARCHAR PRIMARY KEY,
        species_name VARCHAR,
        family VARCHAR,
        redlist_category VARCHAR,
        has_dna_species_level BOOL,
        genus_has_dna BOOL,
        family_has_dna BOOL,
        edge_group_name VARCHAR,
        meets_ebp BOOL
    );

    INSERT INTO SpecInfo
        (SELECT DISTINCT
            d.gbif_accepted_id AS gbif_accepted_id,
            d.species_name AS species_name,
            d.family AS family,
            d.redlist_category AS redlist_category,
            d.has_dna_species_level AS has_dna_species_level,
            d.genus_has_dna AS genus_has_dna,
            d.family_has_dna AS family_has_dna,
            e.edge_group_name AS edge_group_name,
            g.meets_ebp AS meets_ebp
        FROM tabular.dna d
        LEFT OUTER JOIN tabular.edge e
            ON d.gbif_accepted_id = e.gbif_accepted_id
        LEFT OUTER JOIN
        (SELECT gbif_accepted_id,
            BOOL_OR
            ((ebp_standard_criteria LIKE '%%6.7%%'
                OR ebp_standard_criteria LIKE '%%6.C%%')
                AND ebp_standard_criteria IS NOT NULL) AS meets_ebp
        FROM tabular.goat GROUP BY gbif_accepted_id) g
        ON d.gbif_accepted_id = g.gbif_accepted_id);
""")

# Create SpecSystems table
print('Building SpecSystems table...')

MAIN_CON.execute("""
    CREATE TABLE SpecSystems AS
       (SELECT DISTINCT gbif_accepted_id,
        CASE
            WHEN systems LIKE '%Terrestrial%' THEN 'Terrestrial'
            WHEN systems LIKE '%Marine%' THEN 'Marine'
            WHEN systems LIKE '%Freshwater%' THEN 'Freshwater'
            ELSE 'Unknown'
        END AS system
        FROM tabular.iucn);
""")

# Create H3Centroids table
print('Building H3Centroids...')

MAIN_CON.execute("""
CREATE TABLE H3Centroids (
    h3_index VARCHAR PRIMARY KEY,
    latitude DOUBLE,
    longitude DOUBLE,
    is_land BOOLEAN DEFAULT false,
    is_sea BOOLEAN DEFAULT false
)
""")

for res in ('3', '7'):
    parquet = os.path.join(DATA_DIR, f'h3_res{res}_species.parquet')

    # Bulk insert centroids
    print(f'Adding Centroids of res{res} cells...')

    centroid_rows = []
    for row in MAIN_CON.execute(
            f"SELECT DISTINCT h3_index FROM read_parquet('{parquet}')"
    ).fetchall():
        idx = row[0]
        lat, lng = h3.cell_to_latlng(str(idx))
        centroid_rows.append((str(idx), float(lat), float(lng)))
    if centroid_rows:
        MAIN_CON.executemany(
            "INSERT OR IGNORE INTO H3Centroids (h3_index, latitude, longitude) VALUES (?, ?, ?)",
            centroid_rows,
        )

    # Create H3Res3Species and H3Res7Species
    print(f'Building H3Res{res}Species table...')

    MAIN_CON.execute(f"""
        CREATE TABLE H3Res{res}Species (
            h3_index VARCHAR PRIMARY KEY,
            gbif_ids VARCHAR[]
        )
    """)
    MAIN_CON.execute(f"""
        INSERT INTO H3Res{res}Species
           (SELECT h3_index,
                list_transform(gbif_accepted_ids, x -> CAST(x AS VARCHAR)) AS gbif_ids
            FROM read_parquet('{parquet}'))
    """)

# ---------------------------------------------------------------------------
# Classify each H3 cell as land or sea
# ---------------------------------------------------------------------------
# Land if the cell *touches* land at all.  Sea if the cell *touches* sea
# at all.  This means coastal cells (at any resolution) are BOTH land and sea.
# Freshwater is part of the land polygon, so freshwater cells are land too.
print("Classifying H3 cells as land/sea …")

_borders_gdf = gpd.read_parquet(BORDERS_PATH)
_land_geom = _borders_gdf.geometry.union_all()   # single (Multi)Polygon

_all_cells = MAIN_CON.execute(
    "SELECT h3_index FROM H3Centroids"
).fetchall()

_land_count = 0
for (_idx,) in _all_cells:
    # Build the hexagon polygon from H3 cell boundary vertices
    boundary = h3.cell_to_boundary(_idx)           # list of (lat, lng) tuples
    ring = [(lng, lat) for lat, lng in boundary]    # shapely uses (x=lng, y=lat)
    cell_poly = Polygon(ring)

    # Overlap means it's both
    is_land = _land_geom.intersects(cell_poly)
    is_sea = not _land_geom.contains(cell_poly)

    MAIN_CON.execute(
        "UPDATE H3Centroids SET is_land = ?, is_sea = ? WHERE h3_index = ?",
        [is_land, is_sea, _idx],
    )
    if is_land:
        _land_count += 1

_total = len(_all_cells)
print(f"  {_land_count:,} / {_total:,} cells classified as land.")

MAIN_CON.close()
print("Done building Ark-IV database.\n")
