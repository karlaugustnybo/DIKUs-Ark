# Flask + DuckDB + Deck.gl Implementation Plan

## Context
- The original Flask app used `Scss(app)` to compile `styles.scss` → `styles.css`, with a dark theme, `layout.html`, `index.html`, `table.html`, `map.html`, and `update.html`.
- The new canonical data source is `denmark.duckdb` (746 species, H3 hex grids at Res 3 and Res 7).
- The Marimo notebook `denmark_maps.py` already demonstrates the LOD map with weight sliders and deck.gl.
- We want to port that functionality into the existing Flask app with **absolutely minimal changes** to the original HTML/CSS styling.

---

## Phase 1: Dependency Setup

- Add `flask>=3.1.1` and `duckdb>=1.5.2` to `pyproject.toml`.
- Run `uv sync` to install.
- Keep `flask_scss` (already used in the original app).

---

## Phase 2: Backend (`app.py`)

### Remove
- `flask_sqlalchemy`, `SQLAlchemy`, SQLite URI, `Genome` model.
- `/delete/<id>/` and `/update/<id>/` routes (they are tied to the old SQLite table).

### Add
- DuckDB read-only connection helper pointing to `denmark_prototype/denmark.duckdb`.

### Routes
- **`/` (index)**  
  Query DuckDB for basic stats (species total, without DNA, critically endangered, res3 cells). Render `index.html` with original styling.
- **`/data/table/`**  
  Query `merged_species` from DuckDB (species name, family, IUCN status, threat score, DNA coverage score, sampling priority).  
  Support sorting and search.  
  **Remove the old "Submit Your Own Data" form** at the bottom of `table.html`.  
  Render `table.html` using the original table structure, just with real column headers and data.
- **`/data/map/`**  
  Query `h3_res3_metrics` (coarse) or `h3_res7_metrics` (fine) depending on user selection.  
  Compute score per cell using **8 weight sliders**: CR, EN, VU, NT, DD, LC, Missing Species DNA, Missing Genus DNA.  
  Default weights = notebook defaults: `CR=4, EN=3, VU=2, NT=1, DD=2, LC=0.1, sp=2, gen=3`.  
  Pre-calculate hexagon boundary coordinates via `h3.cell_to_boundary`.  
  Apply inferno-like colour mapping based on score fraction.  
  Pass data as a JSON blob to the template.  
  **No zoom / lat / lon sliders** — handled by map navigation (deck.gl `viewState`).

---

## Phase 3: Templates (Minimal Changes)

- **`layout.html`** — keep original.
- **`index.html`** — keep original text and links.  
  Optionally inject small stats (species total, without DNA) into the hero area, but preserve all existing copy and styling.
- **`table.html`** — keep original `<h1>`, `<h3>`, table structure, and bottom links.  
  Replace header row with real columns (*Species Name, Family, IUCN Status, Threat Score, DNA Coverage Score, Sampling Priority*).  
  Replace `<tbody>` loop with data from DuckDB.  
  **Delete the "Submit Your Own Data" form**.
- **`map.html`** — keep original `<h1>` and `<h3>`.  
  Replace the `<img src="{{url_for('static', filename='imgs/heatmap_ex.png')}}">` with a `<div id="map"></div>`.  
  Embed a small `<script>` block that loads **deck.gl from CDN**, reads the JSON data Flask passes, and renders an `H3HexagonLayer` with popups.  
  Add a small form with **8 weight sliders** (`<input type="range">`) that POST back to `/data/map/`.
- **`update.html`** — delete (no longer used).

### CSS
- **Do not modify `styles.scss` except for one new rule**:
  ```scss
  #map {
    width: 70%;
    height: 500px;
    margin: auto;
  }
  ```
- Keep the rest of the original dark theme, typography, and buttons intact.

---

## Phase 4: Map Details

### Data Flow
1. User visits `/data/map/` with optional `GET` params for weights and resolution (`res3` or `res7`).
2. Flask queries the appropriate H3 metrics table from DuckDB.
3. For each row, compute `score = sum(metric * weight)`.
4. Normalize to `frac = score / max_score`.
5. Convert to RGB using a piecewise inferno-like function.
6. Use `h3.cell_to_boundary(h3_index)` to get hexagon boundary coordinates.
7. Build a JSON list: `[{h3_index, score, color: [r,g,b], boundary: [[lat1,lng1], [lat2,lng2], ...]}, ...]`.
8. Render `map.html`, passing JSON into a `const data = ...` variable.
9. JavaScript creates a `new deck.DeckGL(...)` instance with an `H3HexagonLayer` using the JSON data, plus a `ScatterplotLayer` for popups.
10. The weight slider form is plain HTML outside the deck.gl container; submitting the form reloads the page with updated weights.

### Interaction
- **Pan / zoom** directly on the deck.gl map.
- **Resolution toggle** (`res3` vs `res7`) via a radio button group in the weight form, defaulting to `res7`.
- **Weight sliders** for CR, EN, VU, NT, DD, LC, Missing Species DNA, Missing Genus DNA.
- **Popups** on hover showing score + breakdown.

---

## Phase 5: Run & Verify

1. Start Flask: `python app/app.py`
2. Smoke-test:
   - `/` → renders homepage with stats.
   - `/data/table/` → renders table with real species data; search and sort work.
   - `/data/map/` → renders deck.gl map with H3 hexagons; sliders update correctly.
3. Confirm no 500 errors and no console errors in browser.

---

## Done
