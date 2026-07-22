# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "marimo",
#     "duckdb",
#     "numpy",
#     "matplotlib",
#     "pandas",
#     "pyarrow",
#     "h3>=4.1",
#     "lonboard",
# ]
# ///

"""Ark-IV — interactive marimo notebook.

Replicates the Flask webapp's three main views:
  1. Summary statistics on the homepage-style hero.
  2. A sortable, searchable species table with customisable scoring weights.
  3. An interactive H3 hexagon heatmap with a click-to-inspect cell details panel.

Run interactively with:
    uv run marimo edit ark_iv.py
"""

import marimo

__generated_with = "0.23.8"
app = marimo.App(width="medium")


@app.cell
def _():
    import os

    import re

    import duckdb
    import matplotlib
    import marimo as mo
    import numpy as np
    import pandas as pd
    import pyarrow as pa
    from lonboard import H3HexagonLayer, Map

    return H3HexagonLayer, Map, duckdb, matplotlib, mo, np, os, pd, re


@app.cell
def _(os):
    # Paths and defaults -------------------------------------------------
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    cache_path = os.path.join(data_dir, "precomputed_cache.duckdb")

    DEFAULT_WEIGHTS = {
        "cr": 4.0,
        "en": 3.0,
        "vu": 2.0,
        "nt": 1.0,
        "dd": 2.0,
        "lc": 0.1,
        "sp": 2.0,
        "gen": 3.0,
        "fam": 4.0,
        "samp": 0.0,
    }

    WEIGHT_META = {
        "cr": {"label": "Critically Endangered", "step": 0.5},
        "en": {"label": "Endangered", "step": 0.5},
        "vu": {"label": "Vulnerable", "step": 0.5},
        "nt": {"label": "Near Threatened", "step": 0.5},
        "dd": {"label": "Data Deficient", "step": 0.5},
        "lc": {"label": "Least Concern", "step": 0.1},
        "sp": {"label": "Missing Species DNA", "step": 0.5},
        "gen": {"label": "Missing Genus DNA", "step": 0.5},
        "fam": {"label": "Missing Family DNA", "step": 0.5},
        "samp": {"label": "Already Sampled", "step": 0.1},
    }

    IUCN_ORDER = {
        "Critically Endangered": 1,
        "Endangered": 2,
        "Vulnerable": 3,
        "Near Threatened": 4,
        "Data Deficient": 5,
        "Least Concern": 6,
    }
    DNA_ORDER = {
        "Missing Family": 1,
        "Missing Genus": 2,
        "Missing Species": 3,
        "Already Sampled": 4,
    }
    return DEFAULT_WEIGHTS, DNA_ORDER, IUCN_ORDER, WEIGHT_META, cache_path


@app.cell
def _(cache_path, duckdb, re):
    # Pre-load the reference tables and every precomputed H3 aggregate table.
    _con = duckdb.connect(cache_path, read_only=True)
    spec_info = _con.execute("SELECT * FROM SpecInfo").df()

    agg_frames = {}
    for _tbl in (_r[0] for _r in _con.execute("SHOW TABLES").fetchall()):
        _m = re.fullmatch(r"h3_(res\d+)_agg_(.+)", _tbl)
        if _m:
            _res, _sys = _m.group(1), _m.group(2)
            agg_frames[(_res, _sys)] = _con.execute(
                f"SELECT * FROM {_tbl}"
            ).df()

    available_resolutions = sorted({_res for _res, _ in agg_frames})
    available_systems = sorted({_sys for _, _sys in agg_frames})

    stats = {
        "total": _con.execute("SELECT COUNT(*) FROM SpecInfo;").fetchone()[0],
        "critically_endangered": _con.execute(
            "SELECT COUNT(*) FROM SpecInfo WHERE redlist_category = 'Critically Endangered';"
        ).fetchone()[0],
        "edge_species": _con.execute(
            "SELECT COUNT(DISTINCT gbif_accepted_id) FROM SpecInfo WHERE edge_group_name IS NOT NULL;"
        ).fetchone()[0],
        "needs_dna_sampling": _con.execute(
            "SELECT COUNT(*) FROM SpecInfo WHERE has_dna_species_level = false;"
        ).fetchone()[0],
        "res3_cells": _con.execute(
            "SELECT COUNT(*) FROM h3_res3_agg_all;"
        ).fetchone()[0],
        "res7_cells": _con.execute(
            "SELECT COUNT(*) FROM h3_res7_agg_all;"
        ).fetchone()[0],
    }
    _con.close()
    return (
        agg_frames,
        available_resolutions,
        available_systems,
        spec_info,
        stats,
    )


@app.cell
def _(DNA_ORDER, IUCN_ORDER, np, pd, spec_info):
    # Helpers for scoring species and building outputs --------------------
    def build_species_table(weights):
        df = spec_info.copy()

        iucn_weight = df["redlist_category"].map(
            {
                cat: weights[key]
                for cat, key in [
                    ("Critically Endangered", "cr"),
                    ("Endangered", "en"),
                    ("Vulnerable", "vu"),
                    ("Near Threatened", "nt"),
                    ("Data Deficient", "dd"),
                    ("Least Concern", "lc"),
                ]
            }
        ).fillna(0)

        conditions = [
            df["family_has_dna"] == False,
            df["genus_has_dna"] == False,
            df["has_dna_species_level"] == False,
        ]
        choices_label = ["Missing Family", "Missing Genus", "Missing Species"]
        choices_score = [weights["fam"], weights["gen"], weights["sp"]]
        dna_level = np.select(conditions, choices_label, default="Already Sampled")
        dna_score = np.select(conditions, choices_score, default=weights["samp"])

        df = df.assign(
            threat_score=iucn_weight,
            dna_level=dna_level,
            dna_level_score=dna_score,
            priority=iucn_weight * dna_score,
        )

        df["category_rank"] = df["redlist_category"].map(IUCN_ORDER).fillna(7)
        df["dna_rank"] = df["dna_level"].map(DNA_ORDER).fillna(4)

        display = df[
            [
                "species_name",
                "family",
                "redlist_category",
                "threat_score",
                "dna_level",
                "priority",
                "category_rank",
                "dna_rank",
                "gbif_accepted_id",
            ]
        ].copy()
        display.columns = [
            "Species Name",
            "Family",
            "IUCN Status",
            "Threat Score",
            "Missing DNA Level",
            "Priority",
            "_category_rank",
            "_dna_rank",
            "_gbif_id",
        ]
        return display

    def build_cell_species_table(rows, weights):
        records = []
        for name, family, cat, sp_dna, gen_dna, fam_dna in rows:
            if fam_dna is False:
                dna_level = "Missing Family"
            elif gen_dna is False:
                dna_level = "Missing Genus"
            elif sp_dna is False:
                dna_level = "Missing Species"
            else:
                dna_level = "Already Sampled"
            threat = (
                weights["cr"]
                if cat == "Critically Endangered"
                else weights["en"]
                if cat == "Endangered"
                else weights["vu"]
                if cat == "Vulnerable"
                else weights["nt"]
                if cat == "Near Threatened"
                else weights["dd"]
                if cat == "Data Deficient"
                else weights["lc"]
                if cat == "Least Concern"
                else 0
            )
            dna = (
                weights["fam"]
                if dna_level == "Missing Family"
                else weights["gen"]
                if dna_level == "Missing Genus"
                else weights["sp"]
                if dna_level == "Missing Species"
                else weights["samp"]
            )
            records.append(
                {
                    "Species Name": name,
                    "Family": family or "",
                    "IUCN Status": cat or "Not Assessed",
                    "Threat Score": threat,
                    "Missing DNA Level": dna_level,
                    "Priority": threat * dna,
                }
            )
        df = pd.DataFrame(records)
        df["_category_rank"] = df["IUCN Status"].map(IUCN_ORDER).fillna(7)
        df["_dna_rank"] = df["Missing DNA Level"].map(DNA_ORDER).fillna(4)
        return df

    return build_cell_species_table, build_species_table


@app.function
def compute_hex_scores(df, weights):
    return (
        df["crit_endangered_count"].astype(float) * weights["cr"]
        + df["endangered_count"].astype(float) * weights["en"]
        + df["vulnerable_count"].astype(float) * weights["vu"]
        + df["near_threatened_count"].astype(float) * weights["nt"]
        + df["data_deficient_count"].astype(float) * weights["dd"]
        + df["least_concern_count"].astype(float) * weights["lc"]
        + df["missing_species_dna"].astype(float) * weights["sp"]
        + df["missing_genus_dna"].astype(float) * weights["gen"]
        + df["missing_family_dna"].astype(float) * weights["fam"]
    )


@app.cell
def _(DEFAULT_WEIGHTS, WEIGHT_META, mo):
    # Weight controls -----------------------------------------------------
    def make_weight_widget(key):
        meta = WEIGHT_META[key]
        return mo.ui.slider(
            start=0,
            stop=10,
            step=meta["step"],
            value=DEFAULT_WEIGHTS[key],
            label=meta["label"],
            include_input=True,
            full_width=True,
        )

    iucn_keys = ["cr", "en", "vu", "nt", "dd", "lc"]
    dna_keys = ["sp", "gen", "fam", "samp"]

    iucn_sliders = {k: make_weight_widget(k) for k in iucn_keys}
    dna_sliders = {k: make_weight_widget(k) for k in dna_keys}

    def weight_panel(sliders):
        return mo.vstack(
            [
                mo.hstack(
                    [s, mo.md(f"**{WEIGHT_META[k]['label']}**")],
                    justify="start",
                )
                for k, s in sliders.items()
            ],
            gap=0.5,
        )

    iucn_ui = mo.accordion(
        {"IUCN Category Weights": weight_panel(iucn_sliders)},
    )
    dna_ui = mo.accordion(
        {"DNA & Coverage Weights": weight_panel(dna_sliders)},
    )

    controls = mo.vstack([iucn_ui, dna_ui], gap=0.5)
    controls
    return dna_sliders, iucn_sliders


@app.cell
def _(available_resolutions, available_systems, mo):
    # System / resolution selectors ---------------------------------------
    _resolution_options = {
        f"{'Large' if _res == 'res3' else 'Small'} H3 cells ({_res})": _res
        for _res in available_resolutions
    }
    _default_resolution = (
        "Large H3 cells (res3)"
        if "res3" in available_resolutions
        else next(iter(_resolution_options))
    )

    _system_options = {"All": "all"}
    for _sys in available_systems:
        _system_options.setdefault(_sys, _sys)

    system_dropdown = mo.ui.dropdown(
        _system_options,
        value="All",
        label="System",
    )
    resolution_dropdown = mo.ui.dropdown(
        _resolution_options,
        value=_default_resolution,
        label="Resolution",
    )
    mo.hstack([system_dropdown, resolution_dropdown], gap=1)
    return resolution_dropdown, system_dropdown


@app.cell
def _(mo):
    # Table controls ------------------------------------------------------
    search_input = mo.ui.text(
        value="",
        label="Search species or family (regex)",
        full_width=False,
    )
    per_page_select = mo.ui.dropdown(
        {"10": 10, "25": 25, "50": 50, "100": 100},
        value="10",
        label="Rows per page",
    )
    mo.hstack([search_input, per_page_select], gap=1)
    return per_page_select, search_input


@app.cell
def _(
    build_species_table,
    dna_sliders,
    iucn_sliders,
    mo,
    per_page_select,
    search_input,
):
    # Reactive data sources, driven by the weight sliders -----------------
    weights = {**{k: v.value for k, v in iucn_sliders.items()}, **{k: v.value for k, v in dna_sliders.items()}}
    table_df = build_species_table(weights)

    query = search_input.value.strip()
    if query:
        filtered = table_df[
            table_df["Species Name"].str.contains(query, regex=True, case=False, na=False)
            | table_df["Family"].str.contains(query, regex=True, case=False, na=False)
        ].copy()
    else:
        filtered = table_df.copy()

    sortable_table = mo.ui.dataframe(
        filtered,
        page_size=per_page_select.value,
    )

    mo.vstack(
        [
            mo.md("## Explore the Table"),
            mo.md(f"**{len(filtered)}** of {len(table_df)} species"),
            sortable_table,
        ],
        gap=1,
    )
    return (weights,)


@app.cell
def _(DEFAULT_WEIGHTS, H3HexagonLayer, Map, agg_frames, matplotlib, mo, np):
    # Create the map once. It is updated reactively by the cell below, so this
    # cell itself does NOT rerun every time a control changes.
    _default_key = ("res3", "all")
    if _default_key not in agg_frames:
        _default_key = next(iter(agg_frames))

    def build_layer(df, weights, resolution):
        _scores = compute_hex_scores(df, weights)
        _mn, _mx = _scores.min(), _scores.max()
        _frac = np.where((_mx - _mn) > 0, (_scores - _mn) / (_mx - _mn), 0.0)
        _rgba = (matplotlib.colormaps["turbo"](_frac)[:, :3] * 255).astype(np.uint8)
        # Webapp uses alpha = 50/255 for semi-transparent hexagons.
        _rgba = np.column_stack([_rgba, np.full(len(_rgba), 50, dtype=np.uint8)])
        # Match the webapp's per-resolution 3-D extrusion height.
        _elev = 2000 if resolution == "res3" else 500
        return H3HexagonLayer.from_pandas(
            df.assign(score=_scores),
            get_hexagon=df["h3_index"],
            get_fill_color=_rgba,
            get_elevation=_elev,
            elevation_scale=1,
            extruded=True,
            pickable=True,
            auto_highlight=True,
            highlight_color=[255, 255, 255, 50],
            coverage=1.0,
        )

    map_layer = build_layer(agg_frames[_default_key], DEFAULT_WEIGHTS, "res3")
    map_widget = Map(layers=[map_layer], height=600, show_tooltip=True)

    mo.vstack(
        [
            mo.md("## Explore the Map"),
            mo.md(
                "The map updates automatically when weights, system or resolution change — "
                "only the deck.gl layer is regenerated, so pan and zoom are preserved."
            ),
            map_widget,
        ],
        gap=1,
    )
    return build_layer, map_widget


@app.cell
def _(
    agg_frames,
    build_layer,
    map_widget,
    resolution_dropdown,
    system_dropdown,
    weights,
):
    # Reactive layer updater.  This cell runs whenever weights/system/resolution
    # change and swaps the data/colours on the existing Map widget.
    _system = system_dropdown.value
    _resolution = resolution_dropdown.value
    _key = (_resolution, _system)
    if _key not in agg_frames:
        _key = ("res3", "all") if ("res3", "all") in agg_frames else next(iter(agg_frames))

    _df = agg_frames[_key]
    map_widget.layers = [build_layer(_df, weights, _resolution)]
    return


@app.cell
def _(agg_frames, mo, resolution_dropdown, system_dropdown, weights):
    # Cell selector -------------------------------------------------------
    _system = system_dropdown.value
    _resolution = resolution_dropdown.value
    _key = (_resolution, _system)
    if _key not in agg_frames:
        _key = ("res3", "all") if ("res3", "all") in agg_frames else next(iter(agg_frames))

    df = agg_frames[_key].copy()
    _cell_score = compute_hex_scores(df, weights)
    df = df.assign(score=_cell_score).sort_values("score", ascending=False)

    top = df.head(200) if _resolution == "res7" else df
    options = {
        f"{row.h3_index}  (score {row.score:.2f})": row.h3_index
        for _, row in top.iterrows()
    }
    selected_h3 = mo.ui.dropdown(options, label="Inspect an H3 cell", searchable=True)
    selected_h3
    return (selected_h3,)


@app.cell
def _(
    build_cell_species_table,
    cache_path,
    duckdb,
    mo,
    resolution_dropdown,
    selected_h3,
    system_dropdown,
    weights,
):
    # Cell details panel --------------------------------------------------
    _resolution = resolution_dropdown.value
    _system = system_dropdown.value
    h3_index = selected_h3.value

    if not h3_index:
        panel = mo.md("Select an H3 cell above to see its species.").callout()
    else:
        res_part = "Res3" if _resolution == "res3" else "Res7"
        sql = f"""
        SELECT
            sp.species_name,
            sp.family,
            sp.redlist_category,
            sp.has_dna_species_level,
            sp.genus_has_dna,
            sp.family_has_dna
        FROM H3{res_part}Species h3,
        UNNEST(h3.gbif_ids) AS u(gbif_id)
        LEFT JOIN SpecInfo sp ON u.gbif_id = sp.gbif_accepted_id
        WHERE h3.h3_index = ?
          AND sp.species_name IS NOT NULL
        """
        params = [h3_index]
        if _system != "all":
            sql += """
            AND ? IN (
                SELECT system FROM SpecSystems s
                WHERE s.gbif_accepted_id = u.gbif_id
            )
            """
            params.append(_system)
        sql += " ORDER BY sp.species_name;"

        _con = duckdb.connect(cache_path, read_only=True)
        rows = _con.execute(sql, params).fetchall()
        _con.close()

        cell_df = build_cell_species_table(rows, weights)

        _cell_stats = {
            "Total": len(cell_df),
            "CR": int((cell_df["IUCN Status"] == "Critically Endangered").sum()),
            "EN": int((cell_df["IUCN Status"] == "Endangered").sum()),
            "VU": int((cell_df["IUCN Status"] == "Vulnerable").sum()),
            "NT": int((cell_df["IUCN Status"] == "Near Threatened").sum()),
            "DD": int((cell_df["IUCN Status"] == "Data Deficient").sum()),
            "LC": int((cell_df["IUCN Status"] == "Least Concern").sum()),
            "Missing species DNA": int((cell_df["Missing DNA Level"] == "Missing Species").sum()),
            "Missing genus DNA": int((cell_df["Missing DNA Level"] == "Missing Genus").sum()),
            "Missing family DNA": int((cell_df["Missing DNA Level"] == "Missing Family").sum()),
        }

        stat_cards = mo.hstack(
            [mo.stat(label=k, value=int(v)) for k, v in _cell_stats.items()],
            gap=0.5,
            wrap=True,
        )
        table_card = mo.ui.dataframe(cell_df, page_size=10)

        panel = mo.vstack(
            [
                mo.md(f"### Cell {h3_index}"),
                stat_cards,
                table_card,
            ],
            gap=1,
        )
    panel
    return


@app.cell
def _(mo, stats):
    # Hero / summary stats ------------------------------------------------
    stat_items = mo.hstack(
        [
            mo.stat(value=stats["total"], label="Species"),
            mo.stat(value=stats["critically_endangered"], label="Critically Endangered"),
            mo.stat(value=stats["edge_species"], label="EDGE Species"),
            mo.stat(value=stats["needs_dna_sampling"], label="Needs DNA Sampling"),
            mo.stat(value=stats["res3_cells"], label="Large H3 Cells"),
            mo.stat(value=stats["res7_cells"], label="Small H3 Cells"),
        ],
        gap=1,
        justify="center",
        wrap=True,
    )

    hero = mo.md(
        """
        # Ark-*IV*
        Extinction should not mean erasure. This notebook helps prioritise where to sample
        DNA, directing conservation effort toward the species that need it most.
        """
    ).callout()

    mo.vstack([hero, stat_items], gap=1)
    return


if __name__ == "__main__":
    app.run()
