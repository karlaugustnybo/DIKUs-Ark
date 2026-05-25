# DIKUs-Ark Denmark Prototype Data

## Files

### spatial/spatial_denmark.parquet
- 1,322 rows (range polygons), 807 unique species across 23 IUCN taxon groups
- Species whose IUCN ranges overlap the Denmark bounding box (8.0–12.5°E, 54.5–57.8°N)
- Geometries are clipped to the bbox (full resolution, no simplification)
- 37 columns including geometry (`geom_wkb` as WKB binary), H3 indices (`h3_res3`, `h3_res7` as int64), IUCN status (`iucn_category`, `redlistCategory`), DNA gap scores (`threat_score`, `dna_coverage_score`, `sampling_priority`), GOAT sequencing fields, and habitat flags (`marine`, `terrestial`, `freshwater`)
- ~86 MB

### tabular/extra_info_denmark.parquet
- 807 rows (one per species), all 33 columns from `merged_gbif`
- Same 807 species as in the spatial file, joined on `id_no` = `internalTaxonId`
- Includes GBIF accepted IDs, IUCN assessment details (`redlistCategory`, `populationTrend`, `systems`, `realm`), GOAT sequencing status, and DNA match metadata
- ~100 KB

## Joining the files

```python
import duckdb

con = duckdb.connect()
con.install_and_load("spatial")

con.execute("""
    CREATE VIEW spatial AS
    SELECT *, ST_GeomFromWKB(geom_wkb) AS geom
    FROM read_parquet('spatial/spatial_denmark.parquet')
""")

con.execute("""
    CREATE VIEW extra_info AS
    SELECT *
    FROM read_parquet('tabular/extra_info_denmark.parquet')
""")

# One species can have multiple range polygons (1,322 rows → 807 species)
# Join on GBIF accepted ID
merged = con.execute("""
    SELECT s.*, t.populationTrend, t.systems, t.realm
    FROM spatial s
    LEFT JOIN tabular t ON s.gbif_accepted_id = t.gbif_accepted_id
""").df()
```
