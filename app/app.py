#!/usr/bin/env python3
"""
Ark-IV — Flask Application
=============================
Main web server for the Denmark DNA-sequencing prioritisation project.

Routes
------
    /                 → homepage (index.html)
    /data/table/      → interactive paginated table (table.html)
    /api/table-data/  → JSON endpoint for table rows, sorting, pagination
    /data/map/        → interactive Deck.gl heat-map (map.html)
    /api/map-data/    → JSON endpoint for map polygons
    /tutorial/        → tutorial page (tutorial.html)

Databases
---------
    precomputed_cache.duckdb
        • dna             – per-species DNA-level flags.
        • edge            – EDGE evolutionary-distinctness data.
        • goat            – Genome of a Taxon metadata.
        • h3_resN_agg_*   – precomputed H3 hexagon aggregates.

Tech stack
----------
    • Flask + Jinja2   – server / templating
    • DuckDB           – zero-config analytical SQL engine
    • H3 + NumPy      – geo polygons & scoring heat-map
"""

from flask import Flask, render_template, request, jsonify
from flask_compress import Compress
from flask_caching import Cache
import duckdb
import numpy as np
import math
import matplotlib
import os


# Defaults for the scoring sliders.  Keys must stay in sync with the HTML
# slider IDs (cr, en, vu, nt, dd, lc, sp, gen, fam, samp) and with
# the column names used in the priority SQL formula below.
DEFAULT_WEIGHTS = {
    'cr':    4.0,   # Critically Endangered
    'en':    3.0,   # Endangered
    'vu':    2.0,   # Vulnerable
    'nt':    1.0,   # Near Threatened
    'dd':    2.0,   # Data Deficient
    'lc':    0.1,   # Least Concern
    'sp':    2.0,   # missing species-level DNA
    'gen':   3.0,   # missing genus-level DNA
    'fam':   4.0,   # missing family-level DNA
    'samp':  0.0,   # Already Sampled weight
}

app = Flask(__name__)

# Compress JSON responses automatically
Compress(app)

# Simple in-memory response cache (60s default, keyed on query string).
# On production swap CACHE_TYPE to 'RedisCache' or 'FileSystemCache'.
cache = Cache(app, config={
    "CACHE_TYPE": "SimpleCache",
    "CACHE_DEFAULT_TIMEOUT": 60,
})

# Pre-computed 256-color Turbo dis LUT (RGB only, uint8).
turbo = matplotlib.colormaps["turbo"]
TURBO_RAW = (turbo(np.linspace(0, 1, 256))[:, :3] * 255).astype(np.uint8)
TURBO_LUT = TURBO_RAW


# __dir__ is the folder that contains this app.py file.
__dir__ = os.path.dirname(os.path.abspath(__file__))

# Connect to precomputed cache
CACHE_PATH = os.path.join(__dir__, '..', 'data', 'precomputed_cache.duckdb')
MAIN_CON = duckdb.connect(CACHE_PATH, read_only=True)

def get_con():
    """
    Return a read-only DuckDB connection.
    Called inside each route so the DB stays isolated per request.
    """
    return MAIN_CON.cursor()


def read_weights():
    """Parse weight overrides from the current Flask request query string."""
    weights = {}
    for key, default in DEFAULT_WEIGHTS.items():
        try:
            weights[key] = float(request.args.get(key, default))
        except (ValueError, TypeError):
            weights[key] = default
    return weights


# ---------------------------------------------------------------------------
# 3. Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    """
    Homepage.
    Pulls summary stats from both DuckDB databases and renders index.html.
    """
    con = get_con()
    stats = {
        # --- tabular DB (species / IUCN / EDGE / GoaT) ---
        'total': con.execute(
            "SELECT COUNT(*) FROM SpecInfo;"
        ).fetchone()[0],
        'critically_endangered': con.execute(
            "SELECT COUNT(*) FROM SpecInfo WHERE redlist_category = 'Critically Endangered';"
        ).fetchone()[0],
        'edge_species': con.execute(
            "SELECT COUNT(DISTINCT gbif_accepted_id) FROM SpecInfo WHERE edge_group_name IS NOT NULL;"
        ).fetchone()[0],
        # Currently not used
        # Species whose GoaT record does NOT meet EBP standard criteria 6.7 or 6.C
        # 'goat_without_ebp': con.execute("""
        #     SELECT COUNT(*) FROM SpecInfo
        #     WHERE meets_ebp = false
        # """).fetchone()[0],
        'needs_dna_sampling': con.execute(
            "SELECT COUNT(*) FROM SpecInfo WHERE has_dna_species_level = false;"
        ).fetchone()[0],
        # --- H3 (map hexagons) ---
        'res3_cells': con.execute(
            "SELECT COUNT(*) FROM h3_res3_agg_all;"
        ).fetchone()[0],
        'res7_cells': con.execute(
            "SELECT COUNT(*) FROM h3_res7_agg_all;"
        ).fetchone()[0],
    }
    con.close()
    return render_template('index.html', stats=stats)


# ---------------------------------------------------------------------------
#  TABLE routes
# ---------------------------------------------------------------------------

@app.route('/data/table/', methods=['GET'])
def table():
    """
    Render the table view shell (table.html).

    The actual data is **not** queried here — it is fetched asynchronously
    by the page via JavaScript calls to /api/table-data/.
    We only pass through the initial state so the client-side JS can
    start with the correct search text, sort column, sort direction,
    and current slider values.

    NOTE: weight parameters (cr, en, …) are read from the query
    string on every page load so the user can share a URL with a
    specific scoring-setup and the table will open with those
    weights already applied.
    """
    # Parse optional weight overrides from the query string.
    # Same logic as the map route so the two pages stay consistent.
    weights = {}
    for key, default in DEFAULT_WEIGHTS.items():
        try:
            weights[key] = float(request.args.get(key, default))
        except (ValueError, TypeError):
            weights[key] = default

    return render_template(
        'table.html',
        search=request.args.get('search', ''),
        sort=request.args.get('sort', 'priority'),
        order=request.args.get('order', 'desc'),
        weights=weights,
    )


@app.route('/api/table-data/', methods=['GET'])
def table_data():
    """
    JSON endpoint powering the paginated, sortable table.

    Query parameters
    ----------------
    search    – free-text filter matched against species_name & family.
    sort      – column key to order by.
    order     – 'asc' or 'desc'.
    page      – 1-based page number.
    cr,en,…   – optional slider values that feed into the priority formula.

    Response (JSON)
    ---------------
    {
      "rows":       [[col0, col1, …], …],   // up to 10 rows
      "page":       3,
      "total_pages":42,
      "total":      418
    }
    """
    # ----- read optional scoring weights from query string ---------------
    weights = {}
    for key, default in DEFAULT_WEIGHTS.items():
        try:
            weights[key] = float(request.args.get(key, default))
        except (ValueError, TypeError):
            weights[key] = default
    w = weights

    # ----- pagination / sort parameters ------------------------------------
    # We pile a small number of SQL query parameters into a Python list
    # and use DuckDB's prepared-statement style (`?` placeholders).
    # This prevents SQL-injection while letting us build the query text
    # dynamically for pagination, sorting, and weight injection.
    con = get_con()
    search = request.args.get('search', '').strip()
    sort = request.args.get('sort', 'priority')
    order = request.args.get('order', 'desc')
    page = request.args.get('page', '1')
    try:
        page = int(page)
    except ValueError:
        page = 1
    if page < 1:
        page = 1
    per_page = 10

    # Whitelist allowed sort columns (prevents SQL injection).
    allowed = {
        'species_name', 'family', 'redlist_category',
        'threat_score', 'dna_level', 'priority'
    }
    if sort not in allowed:
        sort = 'species_name'
    order_sql = 'DESC' if order.lower() == 'desc' else 'ASC'

    # Pre-format slider weights so SQL strings render cleanly.
    sp_s   = f"{w['sp']:g}"
    gen_s  = f"{w['gen']:g}"
    fam_s  = f"{w['fam']:g}"

    # Re-usable CASE that maps redlist_category → its slider weight.
    iucn_weight_case = f"""
        CASE redlist_category
            WHEN 'Critically Endangered' THEN {w['cr']}
            WHEN 'Endangered' THEN {w['en']}
            WHEN 'Vulnerable' THEN {w['vu']}
            WHEN 'Near Threatened' THEN {w['nt']}
            WHEN 'Data Deficient' THEN {w['dd']}
            WHEN 'Least Concern' THEN {w['lc']}
            ELSE 0
        END
    """

    # Re-usable CASE that gives each IUCN category a fixed ordinal rank
    # (CR is always 1st, EN 2nd, … LC 6th).  This is used in tie-breakers.
    category_rank_case = """\
        CASE redlist_category
            WHEN 'Critically Endangered' THEN 1
            WHEN 'Endangered' THEN 2
            WHEN 'Vulnerable' THEN 3
            WHEN 'Near Threatened' THEN 4
            WHEN 'Data Deficient' THEN 5
            WHEN 'Least Concern' THEN 6
            ELSE 7
        END
    """

    # Re-usable CASE that gives each DNA-level a fixed ordinal rank
    # (Missing Family highest → Already Sampled lowest).
    dna_rank_case = """\
        CASE
            WHEN family_has_dna = false THEN 1
            WHEN genus_has_dna = false THEN 2
            WHEN has_dna_species_level = false THEN 3
            ELSE 4
        END
    """

    # Re-usable CASE for the numeric DNA-level score (used for sorting
    # and priority).  Each level returns its slider value directly.
    dna_score_num = f"""
        CASE
            WHEN family_has_dna = false THEN {w['fam']}
            WHEN genus_has_dna = false THEN {w['gen']}
            WHEN has_dna_species_level = false THEN {w['sp']}
            ELSE {w['samp']}
        END
    """

    # ---- build dynamic query with inline weights ----
    # All species data now comes from the dna table.
    # threat_score:  slider value for the row's IUCN category.
    # dna_level:     text label naming the highest missing TOL level.
    # dna_level_score: numeric slider value for that highest level
    #                  (hidden column, purely for tie-breaking sort).
    # priority:      threat_score * dna_level_score.
    base = f"""
        SELECT species_name,
               family,
               redlist_category,
               (
                   {iucn_weight_case}
               ) AS threat_score,
                (
                    CASE
                        WHEN family_has_dna = false THEN 'Missing Family (' || '{fam_s}' || ')'
                        WHEN genus_has_dna = false  THEN 'Missing Genus (' || '{gen_s}' || ')'
                        WHEN has_dna_species_level = false THEN 'Missing Species (' || '{sp_s}' || ')'
                        ELSE 'Already Sampled'
                    END
                ) AS dna_level,
                (
                    {dna_score_num}
                ) AS dna_level_score,
                (
                    {iucn_weight_case}
                    * {dna_score_num}
                ) AS priority,
                gbif_accepted_id
               FROM SpecInfo
    """
    params = []
    if search:
        base += " WHERE regexp_matches(species_name, ?) OR regexp_matches(family, ?)"
        like = f"{search}"
        params = [like, like]

    # Total row count (needed for page count calculation).
    count_sql = "SELECT COUNT(*) FROM SpecInfo"
    if search:
        count_sql += " WHERE regexp_matches(species_name, ?) OR regexp_matches(family, ?)"

    total = con.execute(count_sql, params).fetchone()[0]
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * per_page

    # Append ORDER BY … LIMIT … OFFSET.
    if sort == 'threat_score':
        base += f""" ORDER BY threat_score {order_sql},
                       ({category_rank_case}) ASC,
                       species_name ASC"""
    elif sort == 'dna_level':
        base += f""" ORDER BY dna_level_score {order_sql},
                       ({dna_rank_case}) ASC,
                       species_name ASC"""
    elif sort == 'priority':
        base += f""" ORDER BY priority {order_sql},
                       ({category_rank_case}) ASC,
                       species_name ASC"""
    else:
        base += f" ORDER BY {sort} {order_sql}"
    base += f" LIMIT {per_page} OFFSET {offset}"
    rows = con.execute(base, params).fetchall()
    con.close()

    # Round floating score values down to 2 decimals for clean display.
    # The SELECT returns 8 columns; we strip dna_level_score (idx 5)
    # and gbif_accepted_id (idx 7) from the visible row.
    result_rows = []
    for r in rows:
        row_list = [str(c) if c is not None else '' for c in r]
        # Columns 3, 5, 6 are threat_score, dna_level_score, priority (floats).
        # Column 4 is dna_level (string label) and stays untouched.
        for idx in (3, 5, 6):
            try:
                row_list[idx] = f"{float(row_list[idx]):.2f}"
            except (ValueError, IndexError):
                pass
        # Drop hidden columns: gbif_accepted_id (7) first, then dna_level_score (5)
        del row_list[7]
        del row_list[5]
        result_rows.append(row_list)

    return jsonify(
        rows=result_rows,
        page=page,
        total_pages=total_pages,
        total=total,
    )


# ---------------------------------------------------------------------------
#  MAP routes — now backed by precomputed aggregate tables
# ---------------------------------------------------------------------------

# Grid snap step for cache-friendly bounding boxes (centidegree ≈ 1 km).
_GRID_SNAP = 0.5   # degrees — adjust to your data density preference


def _snap(val: float, step: float = _GRID_SNAP) -> float:
    """Round ``val`` down to the nearest multiple of ``step``."""
    return math.floor(val / step) * step


def _snap_bounds(lat_min, lat_max, lon_min, lon_max):
    """Return bbox snapped to a fixed grid so the cache key stays stable."""
    return (
        _snap(lat_min), _snap(lat_max, _GRID_SNAP) + _GRID_SNAP,
        _snap(lon_min), _snap(lon_max, _GRID_SNAP) + _GRID_SNAP,
    )


def _query_map_sql(con, resolution: str, lat_lng_bounds=None, system=None):
    """Return a dict of NumPy arrays from a precomputed aggregate table.

    Uses ``con.execute(...).fetchnumpy()`` to avoid the Pandas round-trip.
    """
    suffix = system if system else 'all'
    table_name = f"h3_{resolution}_agg_{suffix}"
    
    params = None
    if lat_lng_bounds is not None:
        lat_min, lat_max, lon_min, lon_max = lat_lng_bounds
        params = [lat_min, lat_max, lon_min, lon_max]
        sql = (
            f"SELECT * FROM {table_name} "
            f"WHERE latitude BETWEEN ? AND ? "
            f"AND longitude BETWEEN ? AND ?"
        )
    else:
        sql = f"SELECT * FROM {table_name}"
    return con.execute(sql, parameters=params).fetchnumpy()


def _format_detail_records(raw_dict):
    """Convert a ``fetchnumpy()`` dict into plain JSON-serialisable records.

    Returns a list of ``{h3_index, details}`` dicts, where ``details``
    contains the raw per-hexagon counts.  No scores or colours are
    computed – the client does that with the current slider weights.
    """
    h3_indexes = np.array(raw_dict['h3_index'], dtype=object)  # keep Python str
    n = h3_indexes.shape[0]

    def _get(col, dtype=int):
        return raw_dict[col].astype(dtype).tolist()

    return [
        {
            'h3_index': h3_indexes[i],
            'details': {
                'CR': int(raw_dict['crit_endangered_count'][i]),
                'EN': int(raw_dict['endangered_count'][i]),
                'VU': int(raw_dict['vulnerable_count'][i]),
                'NT': int(raw_dict['near_threatened_count'][i]),
                'DD': int(raw_dict['data_deficient_count'][i]),
                'LC': int(raw_dict['least_concern_count'][i]),
                'Missing Species DNA': int(raw_dict['missing_species_dna'][i]),
                'Missing Genus DNA': int(raw_dict['missing_genus_dna'][i]),
                'Missing Family DNA': int(raw_dict['missing_family_dna'][i]),
            }
        }
        for i in range(n)
    ]


@app.route('/data/map/', methods=['GET', 'POST'])
def map():
    """Render the map shell (map.html)."""
    weights = read_weights()
    resolution = request.args.get('resolution', 'res3')
    if resolution not in ('res3', 'res7'):
        resolution = 'res3'

    con = get_con()
    raw = _query_map_sql(con, resolution)
    con.close()

    raw_data = _format_detail_records(raw)

    return render_template(
        'map.html',
        raw_data=raw_data,
        weights=weights,
        resolution=resolution,
        turbo_lut=TURBO_LUT.tolist()
    )


@app.route('/api/map-data/', methods=['GET'])
@cache.cached(query_string=True)
def map_data():
    """JSON endpoint for map polygons.

    Returns raw counts (no scores or colours) for the requested resolution
    and system.
    """
    resolution = request.args.get('resolution', 'res3')
    if resolution not in ('res3', 'res7'):
        resolution = 'res3'

    system = request.args.get('system', '').strip()
    valid_systems = ('Terrestrial', 'Freshwater', 'Marine')
    if system not in valid_systems:
        system = None

    lat_min = request.args.get('lat_min', type=float)
    lat_max = request.args.get('lat_max', type=float)
    lon_min = request.args.get('lon_min', type=float)
    lon_max = request.args.get('lon_max', type=float)

    con = get_con()

    # Viewport clipping for res7; res3 is small enough to always fetch all.
    bounds = None
    if resolution == 'res7' and None not in (lat_min, lat_max, lon_min, lon_max):
        bounds = _snap_bounds(lat_min, lat_max, lon_min, lon_max)

    raw = _query_map_sql(con, resolution, bounds, system=system)
    con.close()

    data = _format_detail_records(raw)
    return jsonify(data=data, resolution=resolution)

@app.route('/api/cell-species/', methods=['GET'])
@cache.cached(query_string=True)
def cell_species():
    """Return all species present in a single H3 cell, plus aggregate stats."""
    resolution = request.args.get('resolution', 'res3')
    if resolution not in ('res3', 'res7'):
        resolution = 'res3'

    h3_index = request.args.get('h3_index', '').strip()
    if not h3_index:
        return jsonify(species=[], stats={}, h3_index=None)

    system = request.args.get('system', '').strip()
    valid_systems = ('Terrestrial', 'Freshwater', 'Marine')
    system_join = ""
    system_filter = ""
    if system in valid_systems:
        system_filter = f"""
            AND '{system}' IN 
               (SELECT system FROM SpecSystems s 
                WHERE s.gbif_accepted_id = u.gbif_id)
        """
        f"AND sys.systems LIKE '%{system}%'"

    con = get_con()
    if resolution == 'res3': new_res = 'Res3'
    else: new_res = 'Res7'
    h3_species = f"H3{new_res}Species"
    sql = f"""
        SELECT
            sp.species_name,
            sp.family,
            sp.redlist_category,
            sp.has_dna_species_level,
            sp.genus_has_dna,
            sp.family_has_dna
        FROM {h3_species} h3,
        UNNEST(h3.gbif_ids) AS u(gbif_id)
        LEFT JOIN SpecInfo sp ON u.gbif_id = sp.gbif_accepted_id
        WHERE h3.h3_index = ?
          {system_filter}
          AND sp.species_name IS NOT NULL
        ORDER BY
            CASE sp.redlist_category
                WHEN 'Critically Endangered' THEN 1
                WHEN 'Endangered' THEN 2
                WHEN 'Vulnerable' THEN 3
                WHEN 'Near Threatened' THEN 4
                WHEN 'Data Deficient' THEN 5
                WHEN 'Least Concern' THEN 6
                ELSE 7
            END,
            sp.species_name;
    """
    rows = con.execute(sql, [h3_index]).fetchall()
    con.close()

    species = []
    stats = {
        'total': 0, 'CR': 0, 'EN': 0, 'VU': 0, 'NT': 0,
        'DD': 0, 'LC': 0,
        'missing_species_dna': 0,
        'missing_genus_dna': 0,
        'missing_family_dna': 0,
    }

    cat_map = {
        'Critically Endangered': 'CR', 'Endangered': 'EN',
        'Vulnerable': 'VU', 'Near Threatened': 'NT',
        'Data Deficient': 'DD', 'Least Concern': 'LC',
    }

    for name, family, cat, sp_dna, gen_dna, fam_dna in rows:
        stats['total'] += 1
        key = cat_map.get(cat)
        if key:
            stats[key] += 1
        if sp_dna is False:
            stats['missing_species_dna'] += 1
        if gen_dna is False:
            stats['missing_genus_dna'] += 1
        if fam_dna is False:
            stats['missing_family_dna'] += 1

        if fam_dna is False:
            dna_level = 'Missing Family'
        elif gen_dna is False:
            dna_level = 'Missing Genus'
        elif sp_dna is False:
            dna_level = 'Missing Species'
        else:
            dna_level = 'Sampled'

        species.append({
            'species_name': name,
            'family': family or '',
            'redlist_category': cat or 'Not Assessed',
            'dna_level': dna_level,
        })

    return jsonify(species=species, stats=stats, h3_index=h3_index)


@app.route('/tutorial/')
def tutorial():
    return render_template('tutorial.html')

@app.route('/about-data/')
def about_data():
    return render_template('about_data.html')


# ---------------------------------------------------------------------------
#  Entry point
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    app.run(debug=False)
    # When the dev server terminates, close the shared read-only
    # connection and emit query-monitoring stats.
    MAIN_CON.close()
