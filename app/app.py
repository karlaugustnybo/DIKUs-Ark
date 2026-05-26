from flask import Flask, render_template, request, url_for, jsonify
from flask_scss import Scss
import duckdb
import numpy as np
import matplotlib
import h3
import os

__dir__ = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(__dir__, '..', 'denmark_prototype', 'denmark.duckdb')

app = Flask(__name__)
Scss(app)

viridis = matplotlib.colormaps["viridis"]

DEFAULT_WEIGHTS = {
    'cr': 4.0,
    'en': 3.0,
    'vu': 2.0,
    'nt': 1.0,
    'dd': 2.0,
    'lc': 0.1,
    'sp': 2.0,
    'gen': 3.0,
}


def get_con():
    return duckdb.connect(DB_PATH, read_only=True)


def build_data(df, weights):
    """Compute scores, colours, and H3 boundaries exactly like the Marimo notebook."""
    w = weights
    scores = (
        df['crit_endangered_count'].values * w['cr']
        + df['endangered_count'].values * w['en']
        + df['vulnerable_count'].values * w['vu']
        + df['near_threatened_count'].values * w['nt']
        + df['data_deficient_count'].values * w['dd']
        + df['least_concern_count'].values * w['lc']
        + df['missing_species_dna'].values * w['sp']
        + df['missing_genus_dna'].values * w['gen']
    ).astype(np.float32)

    s_max = scores.max() if len(scores) > 0 and scores.max() > 0 else 1.0
    fracs = scores / s_max

    rgba = viridis(fracs)
    rgb = (rgba[:, :3] * 255).astype(np.uint8)

    records = []
    for i, row in df.iterrows():
        h3_index = str(row['h3_index'])
        # h3.cell_to_boundary returns ((lat, lng), ...) — PolygonLayer needs [lng, lat]
        boundary = h3.cell_to_boundary(h3_index)
        coords = [[p[1], p[0]] for p in boundary]
        if coords[0] != coords[-1]:
            coords.append(coords[0])

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
            }
        })
    return records, float(s_max)


@app.route('/')
def index():
    con = get_con()
    stats = {
        'total': con.execute("SELECT COUNT(*) FROM merged_species").fetchone()[0],
        'without_dna': con.execute(
            "SELECT COUNT(*) FROM merged_species WHERE has_dna_species_level = false"
        ).fetchone()[0],
        'critically_endangered': con.execute(
            "SELECT COUNT(*) FROM merged_species WHERE redlist_category = 'Critically Endangered'"
        ).fetchone()[0],
        'res3_cells': con.execute("SELECT COUNT(*) FROM h3_res3_metrics").fetchone()[0],
        'res7_cells': con.execute("SELECT COUNT(*) FROM h3_res7_metrics").fetchone()[0],
    }
    con.close()
    return render_template('index.html', stats=stats)


@app.route('/data/table/', methods=['GET'])
def table():
    return render_template(
        'table.html',
        search=request.args.get('search', ''),
        sort=request.args.get('sort', 'species_name'),
        order=request.args.get('order', 'asc'),
    )


@app.route('/api/table-data/', methods=['GET'])
def table_data():
    con = get_con()
    search = request.args.get('search', '').strip()
    sort = request.args.get('sort', 'species_name')
    order = request.args.get('order', 'asc')
    page = request.args.get('page', '1')
    try:
        page = int(page)
    except ValueError:
        page = 1
    if page < 1:
        page = 1
    per_page = 10

    allowed = {
        'species_name', 'family', 'redlist_category',
        'threat_score', 'dna_coverage_score'
    }
    if sort not in allowed:
        sort = 'species_name'
    order_sql = 'DESC' if order.lower() == 'desc' else 'ASC'

    base = """
        SELECT species_name, family, redlist_category,
               threat_score, dna_coverage_score
        FROM merged_species
    """
    params = []
    if search:
        base += " WHERE species_name ILIKE ? OR family ILIKE ?"
        like = f"%{search}%"
        params = [like, like]

    count_sql = "SELECT COUNT(*) FROM merged_species"
    if search:
        count_sql += " WHERE species_name ILIKE ? OR family ILIKE ?"

    total = con.execute(count_sql, params).fetchone()[0]
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * per_page

    base += f" ORDER BY {sort} {order_sql}"
    base += f" LIMIT {per_page} OFFSET {offset}"
    rows = con.execute(base, params).fetchall()
    con.close()

    return jsonify(
        rows=[[str(c) if c is not None else '' for c in r] for r in rows],
        page=page,
        total_pages=total_pages,
        total=total,
    )


@app.route('/data/map/', methods=['GET', 'POST'])
def map():
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
    if resolution == 'res7' and None not in (lat_min, lat_max, lon_min, lon_max):
        df = con.execute(f"""
            SELECT * FROM {table_name}
            WHERE latitude BETWEEN {lat_min} AND {lat_max}
              AND longitude BETWEEN {lon_min} AND {lon_max}
        """).df()
    else:
        df = con.execute(f"SELECT * FROM {table_name}").df()
    con.close()

    data, max_score = build_data(df, weights)
    return jsonify(data=data, max_score=max_score, resolution=resolution)


if __name__ == '__main__':
    app.run(debug=True)
