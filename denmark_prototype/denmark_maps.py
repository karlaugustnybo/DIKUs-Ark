import marimo

__generated_with = "0.23.7"
app = marimo.App(width="full")


@app.cell
def _():
    import marimo as mo
    import duckdb
    import numpy as np
    import pyarrow as pa
    import matplotlib
    from lonboard.colormap import apply_continuous_cmap

    inferno = matplotlib.colormaps["inferno"]

    DB_PATH = "denmark.duckdb"
    con = duckdb.connect(DB_PATH, read_only=True)
    con.execute("INSTALL spatial; LOAD spatial;")

    n_taxa = con.execute("SELECT count(*) FROM iucn_taxa").fetchone()[0]
    n_geoms = con.execute("SELECT count(*) FROM iucn_raw_geometries").fetchone()[0]
    n_h3r3 = con.execute("SELECT count(*) FROM h3_res3_metrics").fetchone()[0]
    n_h3r7 = con.execute("SELECT count(*) FROM h3_res7_metrics").fetchone()[0]
    f"Connected to `{DB_PATH}` — {n_taxa} taxa, {n_geoms} ranges, {n_h3r3} H3-res3 cells, {n_h3r7:,} H3-res7 cells"
    return apply_continuous_cmap, con, inferno, mo, np, pa


@app.cell
def _(mo):
    mo.md(r"""
    # Denmark Prototype — LOD Map

    Level-of-detail architecture with on-the-fly weighted scoring.

    | Zoom | Layer | Source |
    |------|-------|--------|
    | 0–4 | H3 res 3 | `h3_res3_metrics` (static) |
    | 4–15+ | H3 res 7 | `h3_res7_metrics` (viewport query) |
    """)
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## Weight Sliders
    """)
    return


@app.cell
def _(mo):
    weight_cr = mo.ui.slider(0, 10, value=4, step=0.5, label="CR Weight")
    weight_en = mo.ui.slider(0, 10, value=3, step=0.5, label="EN Weight")
    weight_vu = mo.ui.slider(0, 10, value=2, step=0.5, label="VU Weight")
    weight_nt = mo.ui.slider(0, 10, value=1, step=0.5, label="NT Weight")
    weight_dd = mo.ui.slider(0, 10, value=2, step=0.5, label="DD Weight")
    weight_lc = mo.ui.slider(0, 10, value=0.1, step=0.1, label="LC Weight")
    weight_sp_dna = mo.ui.slider(0, 10, value=2, step=0.5, label="Missing Species DNA")
    weight_gen_dna = mo.ui.slider(0, 10, value=3, step=0.5, label="Missing Genus DNA")

    mo.vstack([
        mo.hstack([weight_cr, weight_en, weight_vu]),
        mo.hstack([weight_nt, weight_dd, weight_lc]),
        mo.hstack([weight_sp_dna, weight_gen_dna]),
    ])
    return (
        weight_cr,
        weight_dd,
        weight_en,
        weight_gen_dna,
        weight_lc,
        weight_nt,
        weight_sp_dna,
        weight_vu,
    )


@app.cell
def _(mo):
    mo.md(r"""
    ## Map
    """)
    return


@app.cell
def _(mo):
    zoom_level = mo.ui.slider(0, 15, value=4, step=0.5, label="Zoom Level")
    center_lon = mo.ui.slider(5, 16, value=10, step=0.1, label="Center Longitude")
    center_lat = mo.ui.slider(54, 58, value=56, step=0.1, label="Center Latitude")
    mo.hstack([zoom_level, center_lon, center_lat])
    return center_lat, center_lon, zoom_level


@app.cell
def _(
    apply_continuous_cmap,
    center_lat,
    center_lon,
    con,
    inferno,
    np,
    pa,
    weight_cr,
    weight_dd,
    weight_en,
    weight_gen_dna,
    weight_lc,
    weight_nt,
    weight_sp_dna,
    weight_vu,
    zoom_level,
):
    import math
    from lonboard import Map, H3HexagonLayer
    from lonboard.view_state import MapViewState

    zoom = zoom_level.value
    lon = center_lon.value
    lat = center_lat.value

    w_cr = weight_cr.value
    w_en = weight_en.value
    w_vu = weight_vu.value
    w_nt = weight_nt.value
    w_dd = weight_dd.value
    w_lc = weight_lc.value
    w_sp = weight_sp_dna.value
    w_gen = weight_gen_dna.value

    def score_df(df):
        return (
            df["crit_endangered_count"] * w_cr
            + df["endangered_count"] * w_en
            + df["vulnerable_count"] * w_vu
            + df["near_threatened_count"] * w_nt
            + df["data_deficient_count"] * w_dd
            + df["least_concern_count"] * w_lc
            + df["missing_species_dna"] * w_sp
            + df["missing_genus_dna"] * w_gen
        ).values.astype(np.float32)

    def make_h3_layer(df):
        scores = score_df(df)
        s_max = scores.max() if len(scores) > 0 and scores.max() > 0 else 1.0
        colors = apply_continuous_cmap(scores / s_max, inferno)
        colors = np.column_stack([colors, np.full(len(colors), 50, dtype=np.uint8)])

        tbl = pa.table({
            "h3_cell": pa.array(df["h3_index"].values),
            "score": pa.array(scores),
        })
        return H3HexagonLayer(
            table=tbl,
            get_hexagon=tbl["h3_cell"],
            get_fill_color=colors,
            pickable=True,
        )

    if zoom < 4:
        df = con.execute("SELECT * FROM h3_res3_metrics").df()
        layer = make_h3_layer(df)
        info = f"LOD: H3 res 3 ({len(df)} cells, static)"
    else:
        lat_pad = 2.0
        lon_pad = lat_pad / math.cos(math.radians(lat))
        min_lat = lat - lat_pad
        max_lat = lat + lat_pad
        min_lon = lon - lon_pad
        max_lon = lon + lon_pad
        df = con.execute(f"""
            SELECT * FROM h3_res7_metrics
            WHERE latitude BETWEEN {min_lat} AND {max_lat}
              AND longitude BETWEEN {min_lon} AND {max_lon}
        """).df()

        if len(df) == 0:
            df = con.execute("SELECT * FROM h3_res3_metrics").df()
            layer = make_h3_layer(df)
            info = f"LOD: H3 res 3 fallback (no res7 cells in viewport)"
        else:
            layer = make_h3_layer(df)
            info = f"LOD: H3 res 7 ({len(df):,} cells in viewport)"

    view = MapViewState(longitude=lon, latitude=lat, zoom=zoom)
    m = Map(layer, view_state=view, height=500)
    m
    return (info,)


@app.cell
def _(info, mo):
    mo.md(info)
    return


if __name__ == "__main__":
    app.run()
