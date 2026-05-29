#!/usr/bin/env python3
"""
DIKUs ARK — Flask Application
=============================
Main web server for the Denmark DNA-sequencing prioritisation project.

Routes
------
    /                → homepage (index.html)
    /data/table/     → interactive paginated table (table.html)
    /api/table-data/ → JSON endpoint for table rows, sorting, pagination
    /data/map/       → interactive Deck.gl heat-map (map.html)
    /api/map-data/   → JSON endpoint for map polygons

Database
--------
The app reads from a DuckDB file (denmark.duckdb) that contains:
    • merged_species      – one row per species with threat & DNA stats.
    • h3_res3_metrics     – aggregated H3 hexagon metrics (coarse).
    • h3_res7_metrics     – aggregated H3 hexagon metrics (fine).

Tech stack
----------
    • Flask + Jinja2   – server / templating
    • flask_scss       – auto-compile static/styles.scss → styles.css
    • DuckDB           – zero-config analytical SQL engine
    • H3 + NumPy + Matplotlib – geo polygons & scoring heat-map
"""

from flask import Flask, render_template, request, url_for, jsonify
import duckdb
import numpy as np
import matplotlib
import h3
import os
import time
from collections import defaultdict

QUERY_METRICS = defaultdict(lambda: [0, 0.0])

# Monitor a given query from a duckdb connection
def monitor(con: duckdb.DuckDBPyConnection, query: str, parameters=None):
    start_time = time.perf_counter()
    try:
        return con.execute(query, parameters=parameters)
    finally:
        stop_time = time.perf_counter()
        duration = stop_time - start_time
        stats = QUERY_METRICS[query]
        stats[0] += 1
        stats[1] += duration

# Print stats of monitored queries
def print_stats():
    metrics = sorted(QUERY_METRICS.items(), key=(lambda item: item[1][1]), reverse=True)
    print((30*'=') + 'BEGIN STATS FOR QUERIES' + (30*'=') + '\n\n')
    for (query, lst) in metrics:
        print(f'Num calls: {lst[0]}, avg time: {lst[1]/lst[0]}, total time: {lst[1]}')
        print(query)
    print((31*'=') + 'END STATS FOR QUERIES' + (31*'=') + '\n\n')

# ---------------------------------------------------------------------------
# 1. Paths & initialisation
# ---------------------------------------------------------------------------
# __dir__ is the folder that contains this app.py file.
__dir__ = os.path.dirname(os.path.abspath(__file__))

# The DuckDB sits two levels up inside /denmark_prototype/
DB_PATH = os.path.join(__dir__, '..', 'denmark_prototype', 'denmark.duckdb')

app = Flask(__name__)

# Matplotlib colormap used for the heat-map polygons.
viridis = matplotlib.colormaps["viridis"]

# Create main connection to derive cursors from
MAIN_CON = duckdb.connect(DB_PATH, read_only=True)

# Defaults for the scoring sliders.  Keys must stay in sync with the HTML
# slider IDs (cr, en, vu, nt, dd, lc, sp, gen, fam, cov, samp) and with
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
    'cov':   1.0,   # DNA coverage score weight
    'samp':  0.0,   # Already Sampled weight
}


def get_con():
    """
    Return a read-only DuckDB connection.
    Called inside each route so the DB stays isolated per request.
    """
    return MAIN_CON.cursor()


# ---------------------------------------------------------------------------
# 2. Data-building helpers (shared by map & table)
# ---------------------------------------------------------------------------
def build_data(df, weights):
    """
    Compute per-hexagon scores, viridis colours, and H3 lat/lng boundaries.

    Parameters
    ----------
    df : pandas.DataFrame
        Raw rows from ``h3_res3_metrics`` or ``h3_res7_metrics``.
    weights : dict
        User-supplied slider values mapping category → float.

    Returns
    -------
    records : list[dict]
        Each record has:
        - h3_index : str
        - score    : float   (raw weighted sum)
        - frac     : float   (score / max_score, 0-1)
        - color    : [r,g,b,a]
        - contour  : [[lng,lat], ...] closed ring for Deck.gl PolygonLayer
        - details  : dict of the raw constituent counts
    max_score : float
        Maximum score across all rows (used for normalisation).
    """
    w = weights
    # 1. Weighted linear sum of all contributing counts.
    #    The last two (fam, cov) safely default to zero arrays when
    #    the columns are absent (e.g. H3 tables not yet updated).
    missing_family_dna = (
        df['missing_family_dna'].values
        if 'missing_family_dna' in df.columns
        else np.zeros(len(df), dtype=np.float32)
    )
    dna_coverage_score = (
        df['dna_coverage_score'].values
        if 'dna_coverage_score' in df.columns
        else np.zeros(len(df), dtype=np.float32)
    )
    scores = (
        df['crit_endangered_count'].values * w['cr']
        + df['endangered_count'].values * w['en']
        + df['vulnerable_count'].values * w['vu']
        + df['near_threatened_count'].values * w['nt']
        + df['data_deficient_count'].values * w['dd']
        + df['least_concern_count'].values * w['lc']
        + df['missing_species_dna'].values * w['sp']
        + df['missing_genus_dna'].values * w['gen']
        + missing_family_dna * w['fam']
        + dna_coverage_score * w['cov']
    ).astype(np.float32)

    # 2. Normalise so the colour scale spans 0-1 across the whole dataset.
    # Guard: when every cell has a score of 0 (all-zero weights) we set
    # s_max to 1.0 so we don't divide by zero — every cell stays dark.
    s_max = scores.max() if len(scores) > 0 and scores.max() > 0 else 1.0
    fracs = scores / s_max

    # 3. Map fractions to the viridis RGBA ramp (Matplotlib).
    rgba = viridis(fracs)
    rgb = (rgba[:, :3] * 255).astype(np.uint8)

    # 4. Convert each H3 cell index to a closed geo-polygon ring.
    records = []
    for i, row in df.iterrows():
        h3_index = str(row['h3_index'])
        # h3.cell_to_boundary returns ((lat, lng), ...).
        # Deck.gl PolygonLayer expects [lng, lat] — so we swap.
        boundary = h3.cell_to_boundary(h3_index)
        coords = [[p[1], p[0]] for p in boundary]
        if coords[0] != coords[-1]:
            coords.append(coords[0])   # close the ring

        records.append({
            'h3_index': h3_index,
            'score': float(scores[i]),
            'frac': float(fracs[i]),
            'color': [int(rgb[i][0]), int(rgb[i][1]), int(rgb[i][2]), 50],
            'contour': coords,
            'details': {
                'CR': int(row['crit_endangered_count']),
                'EN': int(row['endangered_count']),
                'VU': int(row['vulnerable_count']),
                'NT': int(row['near_threatened_count']),
                'DD': int(row['data_deficient_count']),
                'LC': int(row['least_concern_count']),
                'Missing Species DNA': int(row['missing_species_dna']),
                'Missing Genus DNA': int(row['missing_genus_dna']),
                # Future-proof: include missing-family and coverage
                # scores in the tooltip when the H3 tables gain them.
                'Missing Family DNA': int(
                    row.get('missing_family_dna', 0)),
                'DNA Coverage Score': int(
                    row.get('dna_coverage_score', 0)),
            }
        })
    return records, float(s_max)


# ---------------------------------------------------------------------------
# 3. Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    """
    Homepage.
    Pulls a few quick summary stats from DuckDB and renders index.html.
    """
    con = get_con()
    # Each stat is a single-row, single-col result pulled from DuckDB
    # and packed into a dict so Jinja2 can render them in index.html
    # via `{{ stats.total }}`, `{{ stats.without_dna }}`, etc.
    stats = {
        'total': con.execute(
            "SELECT COUNT(*) FROM merged_species"
        ).fetchone()[0],
        'without_dna': con.execute(
            "SELECT COUNT(*) FROM merged_species WHERE has_dna_species_level = false"
        ).fetchone()[0],
        'critically_endangered': con.execute(
            "SELECT COUNT(*) FROM merged_species WHERE redlist_category = 'Critically Endangered'"
        ).fetchone()[0],
        'res3_cells': con.execute(
            # H3 res 3 cells cover ~220 km edge; shown on the map
            # when the user is zoomed far out (zoom ≤ 7).
            "SELECT COUNT(*) FROM h3_res3_metrics"
        ).fetchone()[0],
        'res7_cells': con.execute(
            # H3 res 7 cells cover ~5 km edge; only loaded for the
            # current viewport when zoomed in (zoom > 7).
            "SELECT COUNT(*) FROM h3_res7_metrics"
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
                ) AS priority
               FROM merged_species
    """
    params = []
    if search:
        base += " WHERE species_name ILIKE ? OR family ILIKE ?"
        like = f"%{search}%"
        params = [like, like]

    # Total row count (needed for page count calculation).
    count_sql = "SELECT COUNT(*) FROM merged_species"
    if search:
        count_sql += " WHERE species_name ILIKE ? OR family ILIKE ?"

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
    rows = monitor(con, base, params).fetchall()
    con.close()

    # Round floating score values down to 2 decimals for clean display.
    # The SELECT returns 7 columns; we strip the hidden dna_level_score
    # (5th index) before sending to the client so it stays 6 visible columns.
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
        # Drop the hidden dna_level_score column (index 5).
        del row_list[5]
        result_rows.append(row_list)

    return jsonify(
        rows=result_rows,
        page=page,
        total_pages=total_pages,
        total=total,
    )


# ---------------------------------------------------------------------------
#  MAP routes
# ---------------------------------------------------------------------------

@app.route('/data/map/', methods=['GET', 'POST'])
def map():
    """
    Render the interactive heat-map page (map.html).

    On first load the full coarse dataset (h3_res3_metrics) is queried,
    scored, coloured, and serialised into the template as JSON so that
    Deck.gl can draw the initial polygon layer immediately.
    """
    # Pull weight overrides from the query string using robust
    # float-conversion fallback (identical logic in table_data).
    weights = {}
    for key, default in DEFAULT_WEIGHTS.items():
        try:
            weights[key] = float(request.args.get(key, default))
        except (ValueError, TypeError):
            weights[key] = default

    # H3 resolution: res3 (coarse) by default.  res7 (fine) is
    # switched automatically by JS when zoom > 7.
    resolution = request.args.get('resolution', 'res3')
    if resolution not in ('res3', 'res7'):
        resolution = 'res3'

    # Map each resolution to its DuckDB table name.
    # `get_con()` is not yet called — the full query string is built
    # below after the table_name is known.
    table_name = 'h3_res3_metrics' if resolution == 'res3' else 'h3_res7_metrics'

    con = get_con()
    df = con.execute(f"SELECT * FROM {table_name}").df()
    con.close()

    data, max_score = build_data(df, weights)
    return render_template(
        'map.html',
        data=data,
        weights=weights,
        resolution=resolution,
        max_score=max_score,
    )


@app.route('/api/map-data/', methods=['GET'])
def map_data():
    """
    JSON endpoint used by the map page when the user zooms past level 7
    or changes a weight slider.

    Returns a fresh set of polygons that match the requested resolution
    and optional bounding box (lat/lon min/max), then scored/coloured
    using the current weights.
    """
    weights = {}
    for key, default in DEFAULT_WEIGHTS.items():
        try:
            weights[key] = float(request.args.get(key, default))
        except (ValueError, TypeError):
            weights[key] = default

    resolution = request.args.get('resolution', 'res3')
    if resolution not in ('res3', 'res7'):
        resolution = 'res3'

    table_name = 'h3_res3_metrics' if resolution == 'res3' else 'h3_res7_metrics'

    con = get_con()
    lat_min = request.args.get('lat_min')
    lat_max = request.args.get('lat_max')
    lon_min = request.args.get('lon_min')
    lon_max = request.args.get('lon_max')

    # For high-resolution data, optional viewport clipping keeps payload small.
    # The four lat/lon bounds are sent by the JS `getBounds()` call when
    # the current resolution is `res7` avoiding a full-table transfer.
    if resolution == 'res7' and None not in (lat_min, lat_max, lon_min, lon_max):
        # Use buffer space around region viewed
        buf_const = 3
        lon_buf = buf_const * (float(lon_max) - float(lon_min))
        lat_buf = buf_const * (float(lat_max) - float(lat_min))

        lon_min = (float(lon_min) - lon_buf + 180.0) % 360.0 - 180.0
        lon_max = (float(lon_max) + lon_buf + 180.0) % 360.0 - 180.0
        lat_min = max(-90.0, min(90.0, float(lat_min) - lat_buf))
        lat_max = max(-90.0, min(90.0, float(lat_max) + lat_buf))
        
        query = f"SELECT * FROM {table_name} " \
                 "WHERE (longitude BETWEEN ? AND ?) " \
                 "AND (latitude BETWEEN ? AND ?)"
        df = monitor(con, query, [
            float(lon_min), 
            float(lon_max),
            float(lat_min), 
            float(lat_max)
        ]).df()
    else:
        # Full dataset for res3 — coarse polygons are lightweight enough
        df = monitor(con, f"SELECT * FROM {table_name}").df()
    con.close()

    # build_data() returns `(records, max_score)` where records is a
    # list[dict] consumable by the JS PolygonLayer constructor.
    data, max_score = build_data(df, weights)
    return jsonify(data=data, max_score=max_score, resolution=resolution)


# ---------------------------------------------------------------------------
#  Entry point
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    app.run(debug=True)

# Close main con connection and print stats 
# from monitored queries.
MAIN_CON.close()
print_stats()