# ---------------------------------------------------------------------------
# This file creates a precomputed map cache for app.py. This way the app can
# access the relevant data without potential for RAM overflow.
# ---------------------------------------------------------------------------

import duckdb
import sys
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. Paths & initialisation
# ---------------------------------------------------------------------------
# __dir__ is the folder that contains this build_cache.py file.
__dir__ = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(__dir__, '..', 'data')

# File for output database
CACHE_PATH = os.path.join(DATA_DIR, 'precomputed_cache.duckdb')
DB_PATH = os.path.join(DATA_DIR, 'Ark-IV.duckdb')

# Check if CACHE_PATH is already in use
if Path(CACHE_PATH).exists():
    # Ask if wanting to rebuild
    while (True):
        rebuild_answer = input('\nPrecomputed cache is already build. Do you wish to reuild? [y/n] ')
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
            os.remove(CACHE_PATH)
            print('Existing cachfile deleted. Rebuilding...')
        except OSError: pass

MAIN_CON = duckdb.connect(CACHE_PATH)

# Attach Ark-IV database compiled from build_db.py
MAIN_CON.execute(f"ATTACH '{DB_PATH}' AS db (READ_ONLY)")

# ---------------------------------------------------------------------------
# 1a. Pre-load H3 spatial data and materialize per-cell aggregations
# ---------------------------------------------------------------------------
# The per-hexagon counts (crit_endangered_count, missing_species_dna, etc.)
# do NOT depend on slider weights – weights are only applied later in NumPy.
# Because tabular data is read-only we materialise the heavy
# UNNEST + JOIN + GROUP BY into real tables so every map request becomes a
# trivial SELECT.

# Create relevant attached tables from Ark-IV database
tables = [
    'H3Res3Species', 
    'H3Res7Species', 
    'SpecInfo', 
    'SpecSystems', 
    'H3Centroids'
]
for table in tables:
    # Copy tables and their structure
    MAIN_CON.execute(f'CREATE TABLE {table} AS (SELECT * FROM db.{table})')


# Materialise the aggregation tables ---------------------------------------------
AGG_TABLES = [
    ('all', ''),
    ('Terrestrial', "LIKE '%Terrestrial%'"),
    ('Freshwater',   "LIKE '%Freshwater%'"),
    ('Marine',       "LIKE '%Marine%'"),
]

print("\nMaterialising aggregate map tables (this may take a moment) …")
for res in ('3', '7'):
    h3_species = f"H3Res{res}Species"
    for suffix, system_filter in AGG_TABLES:
        table_name = f"h3_res{res}_agg_{suffix}"
        
        if system_filter:
            sf = f"""
                WHERE h.gbif_id IN
                   (SELECT gbif_accepted_id FROM SpecSystems t 
                    WHERE t.system {system_filter})
            """
        else:
            sf = ''

        # For Terrestrial and Freshwater, restrict to land cells.
        # For Marine, restrict to sea cells.
        if suffix in ('Terrestrial', 'Freshwater'):
            land_filter = "AND c.is_land = true"
        elif suffix == 'Marine':
            land_filter = "AND c.is_sea = true"
        else:
            land_filter = ""

        MAIN_CON.execute(f"""
            CREATE TABLE {table_name} AS
            SELECT DISTINCT
                h.h3_index,
                h.latitude,
                h.longitude,
                COUNT(*) AS total_species,
                COUNT(*) FILTER (WHERE s.redlist_category = 'Critically Endangered')
                    AS crit_endangered_count,
                COUNT(*) FILTER (WHERE s.redlist_category = 'Endangered')
                    AS endangered_count,
                COUNT(*) FILTER (WHERE s.redlist_category = 'Vulnerable')
                    AS vulnerable_count,
                COUNT(*) FILTER (WHERE s.redlist_category = 'Near Threatened')
                    AS near_threatened_count,
                COUNT(*) FILTER (WHERE s.redlist_category = 'Data Deficient')
                    AS data_deficient_count,
                COUNT(*) FILTER (WHERE s.redlist_category = 'Least Concern')
                    AS least_concern_count,
                COUNT(*) FILTER (WHERE s.has_dna_species_level = false)
                    AS missing_species_dna,
                COUNT(*) FILTER (WHERE s.genus_has_dna = false)
                    AS missing_genus_dna,
                COUNT(*) FILTER (WHERE s.family_has_dna = false)
                    AS missing_family_dna
            FROM (
                SELECT hs.h3_index, u.gbif_id, c.latitude, c.longitude
                FROM {h3_species} hs,
                UNNEST(hs.gbif_ids) AS u(gbif_id)
                INNER JOIN H3Centroids c ON hs.h3_index = c.h3_index
                    {land_filter}
            ) h
            LEFT JOIN SpecInfo s ON h.gbif_id = s.gbif_accepted_id
            {sf}
            GROUP BY h.h3_index, h.latitude, h.longitude;
        """)
        n_rows = MAIN_CON.execute(
            f"SELECT COUNT(*) FROM {table_name}"
        ).fetchone()[0]
        print(f"  Created {table_name}: {n_rows:,} rows")

MAIN_CON.close()
print("Done materialising aggregates.\n")